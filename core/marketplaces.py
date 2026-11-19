"""Connected marketplaces (slice S2): `hub marketplace add|list|remove|update|search`.

A catalog (core/marketplace.py — the format and the bare clone) is connected by the owner: `add <owner/repo | git
address | folder>`. In hub.json — `marketplaces` {name: {source, ref?, commit, updated, official?, error?, pending?,
tried?}}: commit is the last read commit of the catalog, the list and search use it; if an update failed — `error`
and the previous commit keeps working; if never downloaded — `pending` with the reason. The hub connects the official
catalog itself (`update --auto` from hub-up, when it is neither in `marketplaces` nor in `marketplaces_removed`); if
the owner removed it, it does not come back by itself. Catalogs refresh themselves every REFRESH (this is only the
list of entries: the owner installs). Mutating commands need the owner's word (bin/hub); list and search — anyone.
"""
import json
import os
import re
import subprocess
import sys
import time

import platforms
from core import catalog, codestore, embed, i18n, lock, marketplace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the official catalog source, the catalog file at the root of its main: the open core's own catalog repository; a
# team's tree names its own in modules/private.json ("repo": the repository of its modules, the read invitation for
# colleagues is the same) — a file of defaults its embed may bring (core/embed.py); the open flavor cuts that file
# (core/appcode.OPEN_CUT), so the open tree never names it.
# HUB_NO_OFFICIAL_MARKETPLACE (tests, stands) — no official one: the hub downloads nothing by itself, and the official
# names (`agents-hub` and similar) are not read from any source
OPEN_SOURCE = "icons8/agents-hub-plugins"
OFFICIAL_NAME = "agents-hub"


def _pin(repo):
    """The tree's modules/private.json (the core's own or an embed's) as a dict, else {}."""
    try:
        with open(embed.file("modules/private.json", repo) or os.path.join(repo, "modules", "private.json"),
                  encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _github(got):
    # the reader's own rule of a GitHub source (core/marketplace._source): owner/name, the name not dots only
    return isinstance(got, str) and bool(marketplace.GITHUB.fullmatch(got)) and bool(got.split("/")[1].strip("."))


def officials_of(repo):
    """The official catalogs of the core tree repo, from its modules/private.json read once -> (source, team):
    - source — of the catalog named OFFICIAL_NAME: the file's "repo" (owner/name) alone stands instead of the open
      catalog (a team's own, as before Embed API 1.2); otherwise OPEN_SOURCE;
    - team — "repo" with "catalog": {"name", "ref"?} (Embed API 1.2): that repository's catalog next to the open one,
      under its own name and on its branch -> (name, owner/name, ref or None); None — no such key, or a key of the wrong
      shape — no second catalog, and the open one stays under OFFICIAL_NAME (never the meaning before 1.2: a hub that has
      already moved to the pair would lose both official ones; tests/test_marketplaces holds the tree's key to
      the rules)."""
    pin = _pin(repo)
    repo_, want = pin.get("repo"), pin.get("catalog")
    if not _github(repo_):
        return OPEN_SOURCE, None
    if "catalog" not in pin:
        return repo_, None
    name, ref = (want.get("name"), want.get("ref")) if isinstance(want, dict) else (None, None)
    good = isinstance(name, str) and not marketplace._bad_name(name) and not marketplace.reserved(name) and \
        name not in BUILTIN_SOURCES and \
        (ref is None or isinstance(ref, str) and bool(codestore.REF.fullmatch(ref)) and ".." not in ref)
    return OPEN_SOURCE, (name, repo_, ref) if good else None


def source_of(repo):
    return officials_of(repo)[0]


def team_of(repo):
    return officials_of(repo)[1]


# catalogs every hub connects by itself next to the official one (owner 2026-11-17: "the hub must support Claude's and
# Codex's marketplaces out of the box, otherwise users who are not power users get no skills"): name -> source. Third
# party for the hub — no right to revoke what was installed (that is the official one's); removed by the owner — they do
# not come back (marketplaces_removed); their names are read only from these sources. Empty under
# HUB_NO_OFFICIAL_MARKETPLACE, like the official one
BUILTIN_SOURCES = {"claude-plugins-official": "anthropics/claude-plugins-official", "openai-curated": "openai/plugins"}
BUILTIN = {} if os.environ.get("HUB_NO_OFFICIAL_MARKETPLACE") else dict(BUILTIN_SOURCES)
# SOURCE — of the catalog named OFFICIAL_NAME; TEAM — the team's catalog next to the open one, (name, owner/name, ref) or
# None: official as the open one, each revokes what was installed from it. OFFICIAL and TEAM are None under
# HUB_NO_OFFICIAL_MARKETPLACE
SOURCE, _TEAM = officials_of(REPO)
OFFICIAL = None if os.environ.get("HUB_NO_OFFICIAL_MARKETPLACE") else SOURCE
TEAM = None if os.environ.get("HUB_NO_OFFICIAL_MARKETPLACE") else _TEAM
REFRESH = 24 * 3600   # catalogs refresh daily by default (hub.json marketplaces_refresh_hours); a failed download —
                      # the retry is after the same period (or `update`)
CHECK = 600           # hub-up checks whether it is time every 10 minutes
LOG = "marketplaces.log"
AUTO_LOCK = ".auto.lock"   # in <state>/marketplaces/: one `update --auto` at a time
YEAR_3000 = 32503680000    # a later time in hub.json is a typo (and Windows time.localtime cannot go further)
# display words, read in the language of the moment of use (not of the import): marketplace.Words
LABELS = marketplace.Words({"page": "cli.market.kind_page", "process": "cli.market.kind_process",
                            "skills": "cli.market.kind_skills", "themes": "cli.market.kind_themes"})
WORDS = marketplace.Words({**LABELS.keys_of, **marketplace.MODULE_RIGHTS.keys_of})   # entry rights in the catalog's words
KINDS = marketplace.Words({"plugin": "core.marketplaces.kind_plugin", "module": "core.marketplaces.kind_module",
                           "skillset": "core.marketplaces.kind_skillset"})
# NOTHING and NO_CATALOGS are the search's empty answers (hub market search decides whether to print): module attributes
# read through __getattr__, in the language of the moment (see below)
WHAT = {"plugin": "core.marketplaces.kind_plugin", "skillset": "core.marketplaces.kind_skillset",
        "module": "core.marketplaces.kind_module"}   # `what` in installed() rows -> the word for the owner


def __getattr__(name):
    if name == "NOTHING":
        return i18n.t("core.marketplaces.nothing")
    if name == "NO_CATALOGS":
        return i18n.t("core.marketplaces.no_catalogs")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _what(what):
    """The row kind of installed() as the owner reads it."""
    return i18n.t(WHAT[what])


def url_of(text, exists=True):
    """A catalog source as the owner gave it -> a git address: `owner/repo` is GitHub, a git address stays as is, a
    folder on disk (for developing one's own catalog) is the full path; exists — the folder must exist. Otherwise
    RuntimeError."""
    t = str(text).strip()
    if not t or t.startswith("-") or re.search(r"[\s\x00-\x1f]", t):
        raise RuntimeError(i18n.t("core.marketplaces.src_hint"))
    if t.lower().startswith("file:"):
        raise RuntimeError(i18n.t("core.marketplaces.src_file_url"))
    if codestore.is_local(t) and (t.startswith((".", "~", "/", "\\")) or re.match(r"[A-Za-z]:[\\/]", t)):
        path = os.path.abspath(os.path.expanduser(t))
        if exists and not os.path.isdir(path):
            raise RuntimeError(i18n.t("core.marketplaces.src_no_dir", path=path))
        return path
    bare = re.sub(r"\.git$", "", t)
    if marketplace.GITHUB.fullmatch(t) and bare.split("/")[1].strip("."):
        return f"https://github.com/{bare}.git"
    if marketplace.REMOTE.match(t):
        return t
    raise RuntimeError(i18n.t("core.marketplaces.src_bad", text=t))


def _key(url):
    """An address for comparison: without ".git" and a trailing slash; GitHub case-insensitively (that is how GitHub
    understands it)."""
    u = re.sub(r"\.git$", "", str(url or "").rstrip("/\\"))
    if codestore.is_local(u):
        return os.path.normcase(os.path.realpath(os.path.expanduser(u)))
    return u.lower() if re.match(r"(?:https?://|ssh://git@|git@)github\.com[/:]", u, re.I) else u


def same(a, b):
    return bool(a) and bool(b) and _key(a) == _key(b)


def official_url():
    return url_of(OFFICIAL, exists=False) if OFFICIAL else None


def team_url():
    return url_of(TEAM[1], exists=False) if TEAM else None


def is_official(url):
    """The address is an official catalog's: the one named OFFICIAL_NAME or the team's (TEAM)."""
    return bool(OFFICIAL) and same(url, official_url()) or bool(TEAM) and same(url, team_url())


def builtins():
    """[(name, address, official)] of the catalogs the hub connects by itself: the official ones first."""
    return ([(OFFICIAL_NAME, official_url(), True)] if OFFICIAL else []) + \
        ([(TEAM[0], team_url(), True)] if TEAM else []) + \
        [(n, url_of(src, exists=False), False) for n, src in BUILTIN.items()]


def builtin_of(name):
    """The address a built-in catalog name belongs to, or None."""
    return next((u for n, u, _ in builtins() if n == name), None)


def ref_of(name):
    """The branch a built-in catalog is read from (the team's "ref"), or None — the default branch."""
    return TEAM[2] if TEAM and name == TEAM[0] else None


def _section(cfg, key="marketplaces", kind=dict):
    """A hub.json section; of the wrong shape — RuntimeError (do not overwrite what the owner wrote by hand)."""
    value = cfg.get(key)
    if value is None:
        return kind()
    if not isinstance(value, kind):
        raise RuntimeError(i18n.t("core.marketplaces.section_not_object" if kind is dict else
                                  "core.marketplaces.section_not_list", section=key))
    return value


def _clean(m):
    """A catalog record as commands read it: strings as strings, time as a finite number; of the wrong shape — none
    (hub.json is edited by hand: a typo brings down neither a command nor hub-up)."""
    if not isinstance(m, dict):
        return {}
    out = {k: m[k] for k in ("source", "ref", "commit", "error", "pending") if isinstance(m.get(k), str) and m[k]}
    out.update({k: m[k] for k in ("updated", "tried") if isinstance(m.get(k), (int, float))
                and not isinstance(m[k], bool) and 0 <= m[k] < YEAR_3000})
    for flag in ("official", "builtin"):
        if m.get(flag) is True:
            out[flag] = True
    return out


def _catalogs(cfg):
    return {n: _clean(m) for n, m in _section(cfg).items()}


def _date(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "—"


# --- what is installed from a catalog -------------------------------------------------------------------------------

def set_key(s):
    """The key of a `sets` record: <entry>@<catalog> for one plugin of a skill set entry, else its name."""
    return f"{s.get('entry')}@{s.get('name')}" if s.get("plugin") else s.get("name")


def _installed_url(source):
    """The source of an installed item (`owner/repo` for GitHub plugins, an address for modules) -> an address to
    compare."""
    s = str(source or "")
    return f"https://github.com/{s}" if marketplace.GITHUB.fullmatch(s) else s   # the comparison strips ".git" at the end


def installed(cfg, name, url, commit, cat):
    """[(entry, what, key, installed commit)] — what was installed per catalog entries: the install's `market` field,
    and without it (old records) the same source and subdir. An install from another catalog (`market`) is not its."""
    exts = cfg.get("extensions") if isinstance(cfg.get("extensions"), dict) else {}
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    mods = cfg.get("modules") if isinstance(cfg.get("modules"), dict) else {}
    sources = mods.get("sources") if isinstance(mods.get("sources"), list) else []
    rows = [("plugin", k, x.get("source"), x) for k, x in exts.items() if isinstance(x, dict)]
    rows += [("skillset", set_key(x), x.get("source"), x) for x in sets if isinstance(x, dict)]
    rows += [("module", x.get("git"), x.get("git"), x) for x in sources if isinstance(x, dict)]
    out = []
    for e in cat["entries"]:
        src = marketplace.source_of(url, commit, e)
        for what, key, source, x in rows:
            if (what == "module") != (e["type"] == "module"):
                continue
            alike = same(_installed_url(source), src["url"]) and (x.get("subdir") or None) == src.get("subdir")
            market = x.get("market")
            hit = alike if market is None else market == name and (x["entry"] == e["name"] if "entry" in x else alike)
            if hit:
                out.append((e, what, key, x.get("commit")))
    return out


def readable(state_dir, cfg):
    """[(name, hub.json record, catalog)] of readable catalogs: the official one first (its entries take precedence),
    then by name. Read once per pass: each catalog is a `git show` of its commit. A hub.json section of the wrong shape
    (edited by hand) — no catalogs: the old plugin path does not fail because of it (`hub marketplace` and the owner
    fix it)."""
    out = []
    try:
        mps = _catalogs(cfg)
    except RuntimeError:
        return []
    for n, m in mps.items():
        cat, _ = _read(state_dir, n, m)
        if cat is not None:
            out.append((n, m, cat))
    return sorted(out, key=lambda x: (not (x[1].get("official") and is_official(x[1].get("source"))), x[0]))


def official_ready(state_dir, cfg, cats=None):
    """The official catalog is connected and readable. Nothing in this core asks it any more (the hub's old list is gone,
    S8); kept for the previous release's market module, which the page server of that release keeps in memory during
    the update's self-check and which imports this module from the new files."""
    return any(m.get("official") and is_official(m.get("source"))
               for _, m, _ in (readable(state_dir, cfg) if cats is None else cats))


def _plugin_entry(state_dir, cfg, url, sub, cats=None):
    """A pinned plugin entry of a readable catalog with this source and subdir -> "<entry>@<catalog>" or None."""
    for n, m, cat in readable(state_dir, cfg) if cats is None else cats:
        for e in cat["entries"]:
            src = marketplace.source_of(m["source"], m["commit"], e)
            if e["type"] == "plugin" and e["pinned"] and same(src["url"], url) and src.get("subdir") == (sub or None):
                return f"{e['name']}@{n}"
    return None


def entry_for(state_dir, cfg, key, cats=None):
    """An installed plugin key -> "<entry>@<catalog>" by which the catalog installs and updates it, or None. If it was
    installed from a catalog — its `market` and `entry`; an old record (`hub market`, without `market`) — the same
    source and subdir in a readable catalog (section 4: "from agents-hub" on reading, `market` is written with the next
    install). cats — readable()."""
    exts = cfg.get("extensions") if isinstance(cfg.get("extensions"), dict) else {}
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    rec = exts.get(key) if isinstance(exts.get(key), dict) else next(
        (x for x in sets if isinstance(x, dict) and set_key(x) == key), None)
    if not isinstance(rec, dict) or not isinstance(rec.get("source"), str) or not rec["source"]:
        return None
    if isinstance(rec.get("market"), str) and isinstance(rec.get("entry"), str) and rec["market"] and rec["entry"]:
        return f"{rec['entry']}@{rec['market']}"
    sub = marketplace._path(rec["subdir"]) if isinstance(rec.get("subdir"), str) else ""
    return None if sub is None else _plugin_entry(state_dir, cfg, _installed_url(rec["source"]), sub or None, cats)


def entry_for_repo(state_dir, cfg, full, cats=None):
    """A GitHub repo `owner/repo` (the former `hub market install`) -> a pinned catalog entry with it at the root or
    None."""
    return _plugin_entry(state_dir, cfg, _installed_url(full), None, cats) \
        if marketplace.GITHUB.fullmatch(str(full)) else None


def version_order(a, b):
    """Plugin versions "1.15.0" and "1.2.1" -> 1 (a is newer), -1, 0; not dotted numbers — None (nothing to compare)."""
    va, vb = (re.match(r"\d+(?:\.\d+)*", str(v or "").strip()) for v in (a, b))
    if not va or not vb:
        return None
    ta, tb = (tuple(map(int, v.group().split("."))) for v in (va, vb))
    n = max(len(ta), len(tb))
    ta, tb = ta + (0,) * (n - len(ta)), tb + (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def _installation(cfg, what, key):
    """The install record in hub.json by a key from installed() (an extension or a skill set) or {}."""
    if what == "plugin":
        exts = cfg.get("extensions") if isinstance(cfg.get("extensions"), dict) else {}
        return exts.get(key) if isinstance(exts.get(key), dict) else {}
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    return next((x for x in sets if isinstance(x, dict) and set_key(x) == key), {}) if what == "skillset" else {}


def _tree(repo, commit, path):
    r = codestore.git(f"--git-dir={repo}", "rev-parse", "-q", "--verify", f"{commit}:{path}" if path else
                      f"{commit}^{{tree}}")
    return r.stdout.strip() if not r.returncode else None


def _newer(state_dir, name, commit, e, have, rec=None):
    """The catalog commit is newer than the installed one: an external entry — another sha; "./path" — another folder
    in the catalog commit. An entry installed before catalogs (rec without `market`, or bound to its entry only later —
    `bound`, _retire) may stand on the author's head, newer than the pinned one: if the catalog version is older than
    the installed one, that is a rollback, not "newer available"; if versions are equal or unknown, the commit decides.
    A record the catalog installed has the version of what it installed: there the commit decides (an entry's `version`
    may lag behind its manifest)."""
    if not e["pinned"] or not have:
        return False
    if rec and (not rec.get("market") or rec.get("bound")) and version_order(e.get("version"), rec.get("version")) == -1:
        return False
    s = e["source"]
    if s["kind"] != "path":
        return have != s["sha"]
    repo = os.path.join(marketplace.catalog_dir(state_dir, name), codestore.REPO_DIR)
    return have != commit and _tree(repo, have, s["path"]) != _tree(repo, commit, s["path"])


# --- commands -------------------------------------------------------------------------------------------------------

def _record(cfg_path, name, url, fields, drop=(), create=False, unremove=False, **if_ref):
    """A catalog record in hub.json under a lock: on top of what is there now (another command may have fixed the
    neighbours). Without create a catalog removed while downloading is not resurrected; a name taken by another source
    is a refusal. unremove — the owner brought the official one back himself: remove it from `marketplaces_removed`.
    if_ref={"ref": R} — write only if the record's ref is still R (what was downloaded by the old ref does not land
    on top of the new one)."""
    with catalog.edit_settings(cfg_path) as raw:
        mps = _section(raw)
        old = mps.get(name)
        if old is not None and not (isinstance(old, dict) and same(old.get("source"), url)):
            raise RuntimeError(i18n.t("core.marketplaces.name_taken", name=name, source=codestore.shown(
                old.get("source") if isinstance(old, dict) else old)))
        if old is None and not create or if_ref and _clean(old).get("ref") != if_ref["ref"]:
            return False
        rec = {k: v for k, v in (old or {}).items() if k not in drop}
        rec.update(fields)
        mps[name] = rec
        raw["marketplaces"] = mps
        if unremove:
            gone = _section(raw, "marketplaces_removed", list)
            if name in gone:
                raw["marketplaces_removed"] = [n for n in gone if n != name]
        return True


def _summary(cat, hint=True):
    n, bad = len(cat["entries"]), len(cat["skipped"])
    if not bad:
        return i18n.t("core.marketplaces.summary", n=n)
    return i18n.t("core.marketplaces.summary_hint" if hint else "core.marketplaces.summary_bad", n=n, skipped=bad,
                  name=cat["name"])


def add(state_dir, cfg_path, text, ref=None, now=None, restart=None, notify=None, events=None):
    """`hub marketplace add <source> [--ref R]` -> lines for the owner. If the same source is already connected —
    update it; if the catalog name is taken by another source — a refusal."""
    now = time.time() if now is None else now
    url = url_of(text)
    mps = _catalogs(catalog.read_settings(cfg_path))
    known = next((n for n, m in mps.items() if same(m.get("source"), url)), None)
    if known is not None:   # a new --ref is written only if the download by it worked: a typo does not break updates
        lines, ok = _refresh(state_dir, cfg_path, known, mps[known], now,
                             ref=ref if ref and ref != mps[known].get("ref") else None, restart=restart,
                             notify=notify, events=events)
        if not ok:
            raise RuntimeError("\n".join(lines))
        return [i18n.t("core.marketplaces.already", name=known, source=codestore.shown(url))] + lines
    official = is_official(url)
    got = marketplace.fetch(state_dir, url, ref, official=official)
    name, commit = got["name"], got["commit"]
    bound = builtin_of(name) if name != OFFICIAL_NAME else None   # the team's name too: only from its own source
    try:
        if bound and not same(bound, url):   # a built-in name from another source: it would stand in for the real one
            raise RuntimeError(i18n.t("core.marketplaces.builtin_name", name=name, source=codestore.shown(bound)))
        _record(cfg_path, name, url, {"source": url, **({"ref": ref} if ref else {}), "commit": commit,
                                      "updated": now, **({"official": True} if official else {}),
                                      **({"builtin": True} if bound and not official else {})},
                drop=("error", "pending", "tried"), create=True, unremove=official or bool(bound))
    except RuntimeError:
        with marketplace._busy(state_dir):   # we just put the clone under this name — the source in hub.json is another
            if same(marketplace._origin(marketplace._repo(state_dir, name)), url):
                codestore.remove(marketplace.catalog_dir(state_dir, name))
        raise
    lines = [i18n.t("core.marketplaces.added", name=name, source=codestore.shown(url), commit=commit[:12],
                    summary=_summary(got["catalog"]))]
    if not official:
        lines.append(i18n.t("core.marketplaces.foreign_code"))
    return lines + [i18n.t("core.marketplaces.whats_in", name=name)]


def adopt(state_dir, cfg_path, now, **hooks):
    """The built-in catalogs (builtins: the official ones, Claude Code's and Codex's) at first start: not in hub.json and
    the owner did not remove it — connect it (the team's on its ref); if the download failed — `pending` with the reason
    (retry at the next update). One that fails does not hold the others. An official one connected now revokes at once
    what was installed from its entries (as each refresh does). -> (lines, ok)."""
    lines, ok = [], True
    for name, url, official in builtins():
        cfg = catalog.read_settings(cfg_path)
        if name in _section(cfg) or name in _section(cfg, "marketplaces_removed", list) or \
                TEAM and name == TEAM[0] and _unretired(_section(cfg).get(OFFICIAL_NAME)):
            continue   # the team's catalog is still the official one under OFFICIAL_NAME (_retire waits): not twice
        flag = {"official": True} if official else {"builtin": True}
        ref = ref_of(name)
        _stray(state_dir, name, url)
        try:
            got = marketplace.fetch(state_dir, url, ref, name=name, official=official)
        except (RuntimeError, OSError) as e:
            _record(cfg_path, name, url, {"source": url, **({"ref": ref} if ref else {}), **flag,
                                          "pending": str(e)[:400], "tried": now}, create=True)
            lines.append(i18n.t("core.marketplaces.official_failed" if official else "core.marketplaces.builtin_failed",
                                name=name, error=e))
            ok = False
            continue
        _record(cfg_path, name, url, {"source": url, **({"ref": ref} if ref else {}), **flag, "commit": got["commit"],
                                      "updated": now}, create=True)
        lines.append(i18n.t("core.marketplaces.official_added" if official else "core.marketplaces.builtin_added",
                            name=name, commit=got["commit"][:12], summary=_summary(got["catalog"])))
        if official:
            lines += ["  " + x for x in _revoke(state_dir, cfg_path, name, **hooks)]
    return lines, ok


def _stray(state_dir, name, url):
    """A clone left under a built-in name by another source (the team's catalog was the official one under that name,
    and its removal was cut off after hub.json was written — _retire) goes before that name is connected."""
    repo = marketplace._repo(state_dir, name)
    if os.path.isfile(os.path.join(repo, "HEAD")) and not same(marketplace._origin(repo), url):
        with marketplace._busy(state_dir):
            codestore.remove(marketplace.catalog_dir(state_dir, name))


def _revoke(state_dir, cfg_path, name, **hooks):
    """An official catalog's revocations over what is installed from it now -> lines (none — nothing changed)."""
    from core import mpinstall
    cfg = catalog.read_settings(cfg_path)
    m = _catalogs(cfg).get(name) or {}
    cat, _ = _read(state_dir, name, m)
    if cat is None or not (m.get("official") and is_official(m.get("source"))):
        return []
    return mpinstall.revoke(cfg_path, name, installed(cfg, name, m["source"], m["commit"], cat), **hooks)


def _entry_of(rows, what, key):
    """The entry of rows (installed()) the record of this kind and key matches, or None."""
    return next((e for e, w, k, _ in rows if w == what and k == key), None)


def _unretired(m):
    """A hub.json record under OFFICIAL_NAME that is the team's catalog from before Embed API 1.2 (its source, whatever
    its ref): _retire has not taken it yet."""
    return bool(TEAM) and isinstance(m, dict) and same(m.get("source"), team_url())


def _carry_removed(raw):
    """The owner removed the official catalog while it was the team's: the team's name stays away too (in raw, under
    the hub.json lock)."""
    gone = _section(raw, "marketplaces_removed", list)
    if OFFICIAL_NAME in gone and TEAM[0] not in gone and TEAM[0] not in _section(raw):
        raw["marketplaces_removed"] = gone + [TEAM[0]]


def _records(raw):
    """[(kind, key, record)] of what is installed: plugins, skill sets, modules (installed() calls them the same)."""
    exts = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
    sets = raw.get("sets") if isinstance(raw.get("sets"), list) else []
    mods = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
    sources = mods.get("sources") if isinstance(mods.get("sources"), list) else []
    return [("plugin", k, x) for k, x in exts.items() if isinstance(x, dict)] + \
        [("skillset", set_key(x), x) for x in sets if isinstance(x, dict)] + \
        [("module", x.get("git"), x) for x in sources if isinstance(x, dict)]


def _retire(state_dir, cfg_path, now, **hooks):
    """Embed API 1.2 on a team's hub: the team's catalog, until now the official one under OFFICIAL_NAME (its "repo"
    replaced the open catalog), becomes the second official one under its own name (TEAM) and OFFICIAL_NAME is the
    open catalog again. Once, while hub.json still has OFFICIAL_NAME from the team's source, and only when both the old
    record's commit and the team's catalog under its new name (its branch) read — otherwise nothing changes, the hub stays
    on the old one and the next due pass tries again:
    - what was installed from the old one without `market` (before catalogs) is bound to its entry by the same source and
      subdir, as _plugin_entry matches it (R0), and marked `bound`: such a plugin may stand on its author's head, newer
      than the catalog, and _newer offers it no rollback;
    - the old record goes, not into `marketplaces_removed`, and the team's is written with its commit (R1); what was
      installed from the old one and whose name the team's list has (of its kind: a module is a module) is the team's
      now; the rest stays under OFFICIAL_NAME, where adopt() then connects the open catalog — its copies of those
      plugins update them in place, so its revocations apply to them, never to the team's;
    - the owner had removed the official one: the team's name goes into `marketplaces_removed` too — nothing they removed
      comes back by itself.
    One write of hub.json under its lock: cut off, it is all old or all new; the old clone goes after it (_stray finishes
    a removal cut off in between). -> lines."""
    if not TEAM or not OFFICIAL:
        return []
    cfg = catalog.read_settings(cfg_path)
    try:
        mps = _catalogs(cfg)
    except RuntimeError:
        return []
    old = mps.get(OFFICIAL_NAME)
    if old is None:   # removed before (TEAM[0] is in hub.json once adopted, so a later removal of the open one is not this)
        if OFFICIAL_NAME in _section(cfg, "marketplaces_removed", list) and TEAM[0] not in mps and \
                TEAM[0] not in _section(cfg, "marketplaces_removed", list):
            with catalog.edit_settings(cfg_path) as raw:
                _carry_removed(raw)
        return []
    if not _unretired(old) or TEAM[0] in mps:
        return []
    cat, _ = _read(state_dir, OFFICIAL_NAME, old)
    if cat is None:
        return []
    url, ref = team_url(), TEAM[2]
    try:   # the team's catalog under its new name, before anything moves to it
        got = marketplace.fetch(state_dir, url, ref, name=TEAM[0], official=True)
    except (RuntimeError, OSError):
        return []
    rows = installed(cfg, OFFICIAL_NAME, old["source"], old["commit"], cat)
    with catalog.edit_settings(cfg_path) as raw:
        mps = _section(raw)
        if not _unretired(mps.get(OFFICIAL_NAME)) or TEAM[0] in mps:
            return []
        team = {(e["name"], e["type"] == "module") for e in got["catalog"]["entries"]}
        for what, key, x in _records(raw):
            e = _entry_of(rows, what, key) if what != "module" and not x.get("market") else None
            if e is not None:
                x.update(market=OFFICIAL_NAME, entry=e["name"], bound=True)
            if x.get("market") == OFFICIAL_NAME and isinstance(x.get("entry"), str) and \
                    (x["entry"], what == "module") in team:
                x["market"] = TEAM[0]
        mps.pop(OFFICIAL_NAME)
        mps[TEAM[0]] = {"source": url, **({"ref": ref} if ref else {}), "official": True, "commit": got["commit"],
                        "updated": now}
        raw["marketplaces"] = mps
        _carry_removed(raw)
    with marketplace._busy(state_dir):
        codestore.remove(marketplace.catalog_dir(state_dir, OFFICIAL_NAME))
    return [i18n.t("core.marketplaces.official_added", name=TEAM[0], commit=got["commit"][:12],
                   summary=_summary(got["catalog"]))] + ["  " + x for x in _revoke(state_dir, cfg_path, TEAM[0], **hooks)]


def _follow(state_dir, cfg_path, **hooks):
    """Two official catalogs (OFFICIAL_NAME and TEAM): a record installed from one whose entry it no longer has, while
    the other has an entry of that name, kind and the record's own source (and subdir), is the other's now (`market`);
    its key, settings and on/off stay. The same source: a same-named entry of other code never takes a record over.
    Only when both read fine (a commit, no error or pending — a catalog only moves forward, so an entry missing from its
    last good commit went after the install); an entry in both or in neither stays where it is. A skill set entry's
    plugins (`plugin`) stay with their entry. Then the catalog it moved to revokes what it revokes. -> lines."""
    if not TEAM or not OFFICIAL:
        return []
    cfg = catalog.read_settings(cfg_path)
    pair = {OFFICIAL_NAME: TEAM[0], TEAM[0]: OFFICIAL_NAME}
    if not any(isinstance(x.get("market"), str) and x["market"] in pair and isinstance(x.get("entry"), str)
               and not x.get("plugin") for _, _, x in _records(cfg)):
        return []   # nothing installed from either: no catalog is read
    try:
        mps = _catalogs(cfg)
    except RuntimeError:
        return []
    have = {}
    for n in pair:
        m = mps.get(n) or {}
        if not (m.get("official") and is_official(m.get("source"))) or m.get("error") or m.get("pending"):
            return []
        cat, _ = _read(state_dir, n, m)
        if cat is None:
            return []
        have[n] = {(e["name"], e["type"] == "module"): e for e in cat["entries"]}
        have[n]["@"] = (m["source"], m["commit"])

    def target(what, x):
        n = x.get("market")
        if not isinstance(n, str) or n not in pair or not isinstance(x.get("entry"), str) or x.get("plugin"):
            return None
        k = (x["entry"], what == "module")
        e = have[pair[n]].get(k)
        if k in have[n] or e is None:
            return None
        src = marketplace.source_of(*have[pair[n]]["@"], e)
        mine = x.get("git") if what == "module" else _installed_url(x.get("source"))
        return pair[n] if same(mine, src["url"]) and (x.get("subdir") or None) == src.get("subdir") else None

    if not any(target(w, x) for w, _, x in _records(cfg)):
        return []
    moved = set()
    with catalog.edit_settings(cfg_path) as raw:
        for what, _, x in _records(raw):
            to = target(what, x)
            if to:
                x["market"] = to
                moved.add(to)
    lines = []
    for n in sorted(moved):
        lines += _revoke(state_dir, cfg_path, n, **hooks)
    return lines


def due(cfg, now):
    """Whether it is time for hub-up to update catalogs: no official one (and it was not removed) or someone's period
    passed."""
    try:
        mps, gone = _catalogs(cfg), _section(cfg, "marketplaces_removed", list)
    except RuntimeError:
        return False
    if any(n not in mps and n not in gone and not (TEAM and n == TEAM[0] and _unretired(mps.get(OFFICIAL_NAME)))
           for n, _, _ in builtins()):
        return True   # (the team's name while the old official waits for _retire: its own period brings the retry)
    period = refresh_of(cfg)
    return any(_stale(m, now, period) for m in mps.values())


def refresh_of(cfg):
    """How often catalogs refresh themselves, seconds: hub.json `marketplaces_refresh_hours` (a positive number),
    otherwise REFRESH."""
    hours = cfg.get("marketplaces_refresh_hours")
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and 0 < hours < 24 * 366:
        return hours * 3600
    return REFRESH


def _stale(m, now, period=REFRESH):
    """A catalog is due for an update: the period since the last attempt has passed (or it is "in the future" — a typo,
    the machine clock). Without a source — never (the owner fixes it)."""
    last = max(m.get("updated", 0), m.get("tried", 0))
    return bool(m.get("source")) and (now - last >= period or last > now + period)


def _refresh(state_dir, cfg_path, n, m, now, ref=None, restart=None, notify=None, events=None):
    """One catalog (m is a _clean record) -> (lines, ok): a fresh commit in hub.json and what of the installed can be
    updated. If the download failed — `error` (or `pending` if there was no commit yet) and `tried`. ref — new from the
    owner: written only together with the commit downloaded by it, a failure does not record it."""
    if not m.get("source"):
        return [i18n.t("core.marketplaces.refresh_no_source", name=n)], False
    official = bool(m.get("official")) and is_official(m["source"])
    if ref is None and TEAM and n == TEAM[0] and same(m["source"], team_url()) and m.get("ref") != TEAM[2]:
        ref = TEAM[2]   # the build moved its catalog to another branch: written only with the commit read from it
    try:
        got = marketplace.fetch(state_dir, m["source"], ref or m.get("ref"), name=n, official=official)
    except (RuntimeError, OSError) as e:
        if ref is None:
            kind = "pending" if not m.get("commit") else "error"
            _record(cfg_path, n, m["source"], {kind: str(e)[:400], "tried": now},
                    drop=("error",) if kind == "pending" else ("pending",))
        if m.get("commit"):
            return [i18n.t("core.marketplaces.refresh_failed_old", name=n, error=e, commit=m["commit"][:12])], False
        return [i18n.t("core.marketplaces.refresh_failed", name=n, error=e)], False
    # without a new ref: the owner may have changed ref while downloading — the old ref's commit is not written over it
    _record(cfg_path, n, m["source"], {"commit": got["commit"], "updated": now, **({"ref": ref} if ref else {})},
            drop=("error", "pending", "tried"), **({} if ref else {"ref": m.get("ref")}))
    was = m.get("commit")
    summary = _summary(got["catalog"])
    lines = [i18n.t("core.marketplaces.refresh_same", name=n, summary=summary, commit=was[:12]) if was == got["commit"]
             else f"{n}: {summary}, {(was or '—')[:12]} → {got['commit'][:12]}"]
    cfg = catalog.read_settings(cfg_path)
    rows = installed(cfg, n, m["source"], got["commit"], got["catalog"])
    if official:   # a revocation by the official one disables what was installed from the entry; a third-party one — a warning
        from core import mpinstall
        lines += ["  " + x for x in mpinstall.revoke(cfg_path, n, rows, restart=restart, notify=notify, events=events)]
    cats = None
    for e, what, key, have in rows:
        rec = _installation(cfg, what, key)
        if e.get("revoked") and not official:
            lines.append("  " + i18n.t("core.marketplaces.revoked_third", catalog=n, entry=e["name"], what=_what(what),
                                         name=key, why=e["revoked"]))
        elif _newer(state_dir, n, got["commit"], e, have, rec) and not e.get("revoked"):
            if rec and not rec.get("market"):   # before catalogs: the hint is from the catalog the core updates from
                cats = readable(state_dir, cfg) if cats is None else cats
                if entry_for(state_dir, cfg, key, cats) != f"{e['name']}@{n}":
                    continue
            lines.append("  " + i18n.t("core.marketplaces.can_update", entry=e["name"], catalog=n, what=_what(what),
                                         name=key))
    return lines, True


def update(state_dir, cfg_path, name=None, now=None, auto=False, **hooks):
    """`hub marketplace update [<catalog>]` -> (lines, ok): a fresh commit of the catalog(s) and what of the installed
    can be updated. auto (hub-up) — the official one at first start and only those that are due (REFRESH); a second such
    process, while the first runs (hub-up restarted in the middle), exits at once."""
    now = time.time() if now is None else now
    if not auto:
        return _update(state_dir, cfg_path, name, now, auto, hooks)
    os.makedirs(marketplace.store_root(state_dir), exist_ok=True)
    held = lock.acquire(os.path.join(marketplace.store_root(state_dir), AUTO_LOCK))
    if held is None:
        return [i18n.t("core.marketplaces.update_running")], True
    with held:
        return _update(state_dir, cfg_path, name, now, auto, hooks)


def _update(state_dir, cfg_path, name, now, auto, hooks):
    lines, ok = (_retire(state_dir, cfg_path, now, **hooks), True) if auto else ([], True)
    if auto:
        more, ok = adopt(state_dir, cfg_path, now, **hooks)
        lines += more
    cfg = catalog.read_settings(cfg_path)
    mps, period = _catalogs(cfg), refresh_of(cfg)
    if name is not None and name not in mps:
        raise RuntimeError(i18n.t("core.marketplaces.no_catalog", name=name))
    names = [name] if name is not None else sorted(mps)
    for n in [x for x in names if _stale(mps[x], now, period)] if auto else names:
        more, good = _refresh(state_dir, cfg_path, n, mps[n], now, **hooks)
        lines += more
        ok = ok and good
    lines += ["  " + x for x in _follow(state_dir, cfg_path, **hooks)]
    if not lines:
        lines.append(i18n.t("core.marketplaces.nothing_to_update" if auto else "core.marketplaces.no_catalogs"))
    return lines, ok


def remove(state_dir, cfg_path, name, uninstall=False, **hooks):
    """`hub marketplace remove <catalog> [--uninstall]`: disconnect a catalog (and its clone from disk); what was
    installed from it keeps working, there are no more updates from the catalog (uninstall — remove that too). The
    official one goes to `marketplaces_removed`: the hub will not bring it back itself."""
    cfg = catalog.read_settings(cfg_path)   # what is installed from the catalog — before the hub.json lock: no git under it
    m = _catalogs(cfg).get(name) or {}
    cat, _ = _read(state_dir, name, m)
    left = installed(cfg, name, m["source"], m["commit"], cat) if cat else []
    removed = []
    if uninstall and name in _catalogs(cfg):
        from core import mpinstall
        exts = cfg.get("extensions") if isinstance(cfg.get("extensions"), dict) else {}
        sets = [s for s in cfg.get("sets") or [] if isinstance(s, dict)] if isinstance(cfg.get("sets"), list) else []
        ours = {k for k, x in exts.items() if isinstance(x, dict) and x.get("market") == name}
        ours |= {set_key(s) for s in sets if s.get("market") == name}
        for key in sorted(k for k in ours if isinstance(k, str)):   # also what the catalog no longer has or it is unreadable
            try:
                removed += mpinstall.uninstall(state_dir, cfg_path, key, **hooks)
            except mpinstall.Error:
                pass   # already removed (an extension and a set of one plugin are one install)
        left = [r for r in left if r[1] == "module" or r[2] not in ours]
    with catalog.edit_settings(cfg_path) as raw:
        mps = _section(raw)
        if name not in mps:
            raise RuntimeError(i18n.t("core.marketplaces.no_catalog", name=name))
        m = _clean(mps.pop(name))
        official = bool(m.get("official")) or is_official(m.get("source")) or bool(m.get("builtin")) or \
            (builtin_of(name) is not None and same(builtin_of(name), m.get("source")))
        if official:
            gone = _section(raw, "marketplaces_removed", list)
            raw["marketplaces_removed"] = gone + ([name] if name not in gone else [])
    if not marketplace._bad_name(name):   # a name written by hand against the rule is never a catalog folder
        with marketplace._busy(state_dir):
            codestore.remove(marketplace.catalog_dir(state_dir, name))
    lines = [i18n.t("core.marketplaces.removed", name=name)] + removed
    if left:
        lines.append(i18n.t("core.marketplaces.remove_left",
                            items=", ".join(f"{_what(what)} {key}" for _, what, key, _ in left)))
    if official:
        lines.append(i18n.t("core.marketplaces.remove_official" if m.get("official") or is_official(m.get("source"))
                            else "core.marketplaces.remove_builtin", name=name, source=codestore.shown(m.get("source") or "")))
    return lines


def _read(state_dir, name, m):
    """(catalog or None, why it is unreadable)."""
    if not m.get("source") or marketplace._bad_name(name):
        return None, i18n.t("core.marketplaces.read_no_source", name=name)
    if not m.get("commit"):
        return None, i18n.t("core.marketplaces.read_not_downloaded",
                            why=m.get("pending") or i18n.t("core.marketplaces.no_attempt"))
    try:
        return marketplace.read(state_dir, name, m["commit"],
                                official=bool(m.get("official")) and is_official(m.get("source"))), None
    except RuntimeError as e:
        return None, i18n.t("core.marketplaces.read_unreadable", error=e)


def _rights(e):
    r = e.get("rights") or {}
    words = [WORDS.get(k, k) + (f": {v}" if isinstance(v, str) else
                                                       f": {', '.join(map(str, v))}" if isinstance(v, list) else "")
             for k, v in r.items() if v]
    return ", ".join(words)


def listing(state_dir, cfg_path, name=None):
    """`hub marketplace list [<catalog>]` -> lines: catalogs (source, commit, entries, when updated) or the entries of
    one (name, kind, rights in the catalog's words, version, whether installed) and skipped ones with the reason."""
    cfg = catalog.read_settings(cfg_path)
    mps = _catalogs(cfg)
    if name is None:
        if not mps:
            return [i18n.t("core.marketplaces.no_catalogs")]
        out = []
        for n, m in sorted(mps.items()):
            cat, why = _read(state_dir, n, m)
            flags = (" · " + i18n.t("core.marketplaces.list_official") if m.get("official") else "") + (
                f" · ✗ {why}" if why else f" · {_summary(cat, hint=False)}") + (
                " · " + i18n.t("core.marketplaces.list_failed", date=_date(m.get("tried")), error=m["error"])
                if m.get("error") else "")
            out.append("  " + i18n.t("core.marketplaces.list_row", name=f"{n:<20}",
                                     source=codestore.shown(m.get("source") or "?"), commit=m.get("commit", "—")[:12],
                                     date=_date(m.get("updated")), flags=flags))
        return out
    if name not in mps:
        raise RuntimeError(i18n.t("core.marketplaces.no_catalog", name=name))
    m = mps[name]
    cat, why = _read(state_dir, name, m)
    if cat is None:
        raise RuntimeError(f"{name}: {why}")
    have = {e["name"]: key for e, _, key, _ in installed(cfg, name, m["source"], m["commit"], cat)}
    out = [f"{name}: {cat.get('description') or codestore.shown(m['source'])} @ {m['commit'][:12]}"]
    for e in cat["entries"]:
        parts = [KINDS[e["type"]], e.get("version") or "", _rights(e),
                 i18n.t("core.marketplaces.list_installed", name=have[e["name"]]) if e["name"] in have else "",
                 "" if e["pinned"] else i18n.t("core.marketplaces.list_unpinned"),
                 i18n.t("core.marketplaces.list_revoked", why=e["revoked"]) if e.get("revoked") else ""]
        out.append(f"  {e['name'] + '@' + name:<32} " + " · ".join(p for p in parts if p))
        if e.get("description"):
            out.append(f"      {e['description'][:120]}")
    out += [f"  ✗ {s['entry']}: {s['why']}" for s in cat["skipped"]]
    if not cat["entries"] and not cat["skipped"]:
        out.append("  " + i18n.t("core.marketplaces.list_empty"))
    return out


def _asks(said, approved):
    """What the new version asks beyond the approved — by the catalog's words (the entry's `rights`) -> [[kind, what]],
    [] — nothing, None — the catalog did not name rights. This is a hint for the list: what the code asks is computed by
    the core from the manifest at install."""
    if not isinstance(said, dict):
        return None
    have = {tuple(p) for p in approved or []}
    runs = set()
    for kind, what in have:
        if kind == "process":
            try:
                run = json.loads(what)
            except ValueError:
                run = what
            runs.add(" ".join(run) if isinstance(run, list) else str(run))
    out = [[k, ""] for k in ("page", "themes") if said.get(k) and (approved is None or (k, "") not in have)]
    if said.get("skills") and (approved is None or not any(k not in ("page", "process", "themes") for k, _ in have)):
        out.append(["skills", ""])
    if said.get("process"):
        cmd = said["process"] if isinstance(said["process"], str) else ""
        if approved is None or not (cmd in runs if cmd else runs):
            out.append(["process", cmd])
    return out


def page(state_dir, cfg_path):
    """"Settings -> Plugins" (slice S5) -> {catalogs, entries}: connected catalogs (source, commit, when updated, how
    many entries, why unreadable) and entries of readable ones — for the screen and search, whether installed (the
    key), whether the catalog has a newer one (newer) and what the new version asks beyond the approved in the catalog's
    words (wider; None — not named). The icon is a core address (/api/marketplaces/icon), the file comes from the
    catalog clone: the page does not go to foreign servers."""
    from urllib.parse import quote
    from core import plugrights
    cfg = catalog.read_settings(cfg_path)
    exts = cfg.get("extensions") if isinstance(cfg.get("extensions"), dict) else {}
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    cats, entries = [], []
    good = readable(state_dir, cfg)   # also for which catalog the core will update an old record from (entry_for)
    read = {n: cat for n, _, cat in good}
    for n, m in sorted(_catalogs(cfg).items()):
        cat, why = (read[n], None) if n in read else _read(state_dir, n, m)
        cats.append({"name": n, "source": codestore.shown(m.get("source") or ""), "commit": m.get("commit") or "",
                     "official": bool(m.get("official")) and is_official(m.get("source")), "updated": m.get("updated"),
                     # an engine's own catalog (Claude Code, Codex): the page lists what was installed from it apart
                     "builtin": bool(m.get("builtin")) or n in BUILTIN_SOURCES,
                     "error": m.get("error"), "problem": why, "description": (cat or {}).get("description"),
                     "entries": len(cat["entries"]) if cat else 0, "skipped": len(cat["skipped"]) if cat else 0})
        if cat is None:
            continue
        have = {}
        for e, what, key, commit in installed(cfg, n, m["source"], m["commit"], cat):
            rec = _installation(cfg, what, key)
            # what was installed before catalogs is "from a catalog" where the core will update it from: a pinned
            # entry, the official catalog first (otherwise the page sends a commit of one catalog, and the core
            # installs from another)
            if what == "module" or rec.get("market") or entry_for(state_dir, cfg, key, good) == f"{e['name']}@{n}":
                have[e["name"]] = (key, commit, rec)
        for e in cat["entries"]:
            s = e["source"]
            out = {"name": e["name"], "catalog": n, "type": e["type"], "pinned": e["pinned"],
                   **{k: e[k] for k in ("version", "description", "category", "author", "tags", "hubs", "rights",
                                        "revoked") if k in e},
                   "icon": f"/api/marketplaces/icon?catalog={quote(n)}&entry={quote(e['name'])}" if e.get("icon") else None}
            if s["kind"] == "github" and not s.get("subdir"):
                out["repo"] = s["github"]   # the same repo under review by link — one row on the page
            if e["name"] in have:
                key, commit, rec = have[e["name"]]
                out["installed"] = key
                if _newer(state_dir, n, m["commit"], e, commit, rec):
                    approved = plugrights.approved_of(exts.get(key), [x for x in sets if isinstance(x, dict)
                                                                       and set_key(x) == key])[0]
                    out.update(newer=True, wider=_asks(e.get("rights"), approved) if e["type"] == "plugin" else None)
            entries.append(out)
    return {"catalogs": cats, "entries": entries}


ICON_MAX = 256 * 1024


def icon(state_dir, cfg_path, name, entry):
    """An entry icon — a file from the catalog clone at its commit -> (bytes, extension) or None: no catalog, entry,
    icon, not a file or larger than ICON_MAX."""
    m = _catalogs(catalog.read_settings(cfg_path)).get(name)
    if not m or marketplace._bad_name(name):
        return None
    cat, _ = _read(state_dir, name, m)
    e = next((x for x in (cat or {}).get("entries", []) if x["name"] == entry and x.get("icon")), None)
    if e is None:
        return None
    repo, obj = marketplace._repo(state_dir, name), f"{m['commit']}:{e['icon']}"
    r = codestore.git(f"--git-dir={repo}", "cat-file", "-t", obj)
    if r.returncode or r.stdout.strip() != "blob":
        return None
    r = codestore.git(f"--git-dir={repo}", "cat-file", "-s", obj)
    if r.returncode or int(r.stdout.strip() or 0) > ICON_MAX:
        return None
    r = codestore.git(f"--git-dir={repo}", "cat-file", "blob", obj, binary=True)
    return None if r.returncode else (r.stdout, os.path.splitext(e["icon"])[1].lower())


def search(state_dir, cfg_path, words):
    """`hub marketplace search [words]` -> lines: entries of all readable catalogs that contain all the words (nothing —
    one line, NOTHING or NO_CATALOGS)."""
    cfg = catalog.read_settings(cfg_path)
    q = [w.lower() for w in words]
    try:
        mps = _catalogs(cfg)
    except RuntimeError as e:   # the section was edited by hand: show the reason
        return [f"✗ {e}"]
    out = []
    for n, m in sorted(mps.items()):
        cat, _ = _read(state_dir, n, m)
        if cat is None:
            continue
        have = {e["name"] for e, *_ in installed(cfg, n, m["source"], m["commit"], cat)}
        for e in cat["entries"]:
            repo = e["source"].get("github") or ""   # a plugin is also named by the author's repo ("install acme/kanban")
            text = " ".join([e["name"], n, KINDS[e["type"]], e.get("description") or "", e.get("category") or "",
                             e.get("author") or "", repo, " ".join(e.get("tags") or [])]).lower()
            if all(w in text for w in q):
                parts = [KINDS[e["type"]], repo or e.get("author") or "", e.get("version") or "",
                         i18n.t("cli.market.installed_mark") if e["name"] in have else "",
                         (e.get("description") or "")[:70]]
                out.append(f"  {e['name'] + '@' + n:<32} " + " · ".join(p for p in parts if p))
    if not out:
        return [i18n.t("core.marketplaces.nothing" if mps else "core.marketplaces.no_catalogs")]
    return out


# --- hub-up -------------------------------------------------------------------------------------------------------------

def kick(state_dir, proc, now=None):
    """A hub-up turn: if catalogs are due — a separate process `hub marketplace update --auto` (network and git are not
    in hub-up); if the previous one still runs — wait for it. -> the process or None. If hub.json is unreadable —
    nothing."""
    if proc is not None and proc.poll() is None:
        return proc
    try:
        cfg = catalog.read_settings(catalog.config_path(state_dir))
    except ValueError:
        return None
    if not isinstance(cfg, dict) or not due(cfg, time.time() if now is None else now):
        return None
    env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
    env["HUB_STATE_DIR"] = state_dir
    os.makedirs(os.path.join(state_dir, "logs"), exist_ok=True)
    with open(os.path.join(state_dir, "logs", LOG), "a", encoding="utf-8") as log:
        log.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.flush()
        return subprocess.Popen([sys.executable, os.path.join(REPO, "bin", "hub"), "marketplace", "update", "--auto"],
                                env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=REPO,
                                **getattr(platforms.current, "no_terminal", lambda: {})())   # getattr: see codestore.git
