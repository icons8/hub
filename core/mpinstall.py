"""Plugins and modules from catalogs (slices S3, S4): `hub marketplace install <entry>[@<catalog>] [--accept]` and
`uninstall`. A module entry (`type: module`) is installed by core/modsrc.from_catalog (a source in `modules.sources`
exactly at the catalog's sha) - after the same revocation and sha checks; below is about plugins.

An entry is installed exactly at the pinned commit (the catalog's `sha`; `./path` - the commit of the catalog itself) into
the storage `<state>/plugins/` (core/codestore.py: a folder per commit, strict check, limits). `install` of one that is
already installed is an update to the catalog's version: the new version's rights (core/plugrights.py) are compared with
the approved ones, and if wider it is not installed without `--accept`, the previous one keeps working. The consent is the
`plugins.consented` event (to the journal and to the owner in Telegram).

The installation is the same as in `hub market`: a record in `extensions` (page, process, themes) and/or `sets` (skills),
plus `market` (where it was installed from), `entry` (the entry name), `subdir`, `commit`, `approved`. The identity of an
installation is the source and subdir: the same code from another catalog is the same installation; and an installation
made from this entry of this catalog (`market`, `entry`) is the same one after the catalog has moved the entry to another
repo (a copy the catalog keeps) - it is updated in place, the note says from where to where. The key is `<author>:<name>`
for GitHub (the author is the owner of the repo it was first installed from; for `./path` - the owner of the catalog
repo), otherwise `<catalog>:<name>`; the key of an installed one never changes. The folder of the earlier storage (`<state>/market/...`) is
removed once the new one is in place.

A revoked entry (`revoked`) is installed only with `--despite-revoked`; the official catalog on update disables what was
installed from a revoked entry (revoke), a third-party one only warns.
"""
import json
import os
import re

import platforms
from core import catalog, codestore, i18n, marketplace, marketplaces, modsrc, plugrights, version

GH_URL = re.compile(r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]+?)(?:\.git)?/?",
                    re.I)
OLD_STORE = "market"   # the earlier plugin storage (modules/market): <state>/market/<account>/<repo>
# record keys written by an installation (the rest of the record is the owner's settings, they survive an update)
FROM_MANIFEST = ("web", "run", "themes", "css", "hub", "version")
# THIRD_PARTY - the warning about a third-party catalog: read through __getattr__ below, in the language of the moment
MANAGED = {"path", "source", "subdir", "commit", "market", "entry", "approved", "revoked", "disabled", *FROM_MANIFEST}


def __getattr__(name):
    if name == "THIRD_PARTY":
        return i18n.t("core.mpinstall.third_party")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class Error(RuntimeError):
    pass


class NeedsConsent(Error):
    def __init__(self, text, wider, shown=None):
        super().__init__(text)
        self.wider, self.shown = wider, shown if shown is not None else [plugrights.shown(w) for w in wider]


def github_of(url):
    """`owner/repo` of a GitHub address (https), otherwise None."""
    m = GH_URL.fullmatch(str(url or ""))
    return f"{m.group(1)}/{m.group(2)}" if m else None


def stored(url):
    """The source in hub.json: GitHub - `owner/repo` (as in `hub market`: the earlier core will update it), otherwise the address."""
    return github_of(url) or url


def url_of(source):
    """The git address from a hub.json source - the same one it was installed with (the storage tells sources apart by address)."""
    s = str(source or "")
    return f"https://github.com/{s}.git" if marketplace.GITHUB.fullmatch(s) else s


def key_for(src_url, catalog_name, name):
    """The key of a new installation: `<author>:<name>` for GitHub, otherwise `<catalog>:<name>` (catalog names are unique on a hub)."""
    gh = github_of(src_url)
    return f"{(gh.split('/')[0] if gh else catalog_name).lower()}:{name}"


def _same(record, url, sub):
    return isinstance(record, dict) and marketplaces.same(url_of(record.get("source")), url) and \
        (record.get("subdir") or None) == (sub or None)


def _from(record, market, entry):
    """The record was installed from this entry of this catalog (its source may have moved since). A skill set entry's
    record (`plugin`, named after the catalog - _skillset) is not a plugin's."""
    return bool(market) and isinstance(record, dict) and record.get("market") == market and \
        record.get("entry") == entry and not record.get("plugin")


def _where(source, sub):
    """A source with its subdir, for the owner's eyes."""
    return f"{source}/{sub}" if sub else str(source)


def _records(cfg, url, sub, market=None, entry=None):
    """(key, extension record or None, set records) of the installation with this source and subdir. market, entry - the
    catalog entry being installed: an installation made from it earlier is the same one, also when the catalog has moved
    the entry to another repo (a copy kept by the catalog) - it is updated in place, not installed a second time."""
    exts = cfg.get("extensions") if isinstance(cfg.get("extensions"), dict) else {}
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    key = next((k for k, x in exts.items() if _same(x, url, sub)), None)
    mine = [s for s in sets if _same(s, url, sub)]
    if key is None and not mine:
        key = next((k for k, x in exts.items() if _from(x, market, entry)), None)
        mine = [s for s in sets if _from(s, market, entry)]
    if key is None and mine and isinstance(mine[0].get("name"), str):
        key = mine[0]["name"]
    return key, exts.get(key) if key in exts else None, mine


def _clash(cfg, key, url, sub, market=None, entry=None):
    """The key is taken by another plugin (an extension or a skill set with another source, not installed from this
    catalog entry) -> a reason or None."""
    exts = cfg.get("extensions") if isinstance(cfg.get("extensions"), dict) else {}
    if key in exts and not _same(exts[key], url, sub) and not _from(exts[key], market, entry):
        return i18n.t("core.mpinstall.clash_ext", name=key, source=exts[key].get("source"))
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    other = next((s for s in sets if isinstance(s, dict) and s.get("name") == key and not _same(s, url, sub)
                  and not _from(s, market, entry)), None)
    return i18n.t("core.mpinstall.clash_set", name=key, source=other.get("source")) if other else None


def card_names(st, ref, name):
    """The owner's answer to card ref (its text carries the start of the question) names this plugin: `x@catalog`,
    `author:x`, `author/repo` - any of the name, the key or the repo will do."""
    from core import tools
    msg = tools.card_answer(st, str(ref or "")) or {}
    text = str(msg.get("text") or "").lower()
    n = str(name or "").strip().lower()
    names = {n, n.split("@")[0], n.split(":")[-1], n.split("/")[-1], re.sub(r"^hub-plugin-", "", n.split("/")[-1])}
    return bool(text) and any(len(x) >= 3 and re.search(rf"(?<![\w-]){re.escape(x)}(?![\w-])", text) for x in names if x)


def find(state_dir, cfg_path, text):
    """`<entry>[@<catalog>]` -> (catalog name, the catalog's hub.json record, catalog, entry). Without @ - the name is one
    across all catalogs, otherwise a refusal with a list."""
    name, _, where = str(text).partition("@")
    cfg = catalog.read_settings(cfg_path)
    mps = marketplaces._catalogs(cfg)
    if where and where not in mps:
        raise Error(i18n.t("core.marketplaces.no_catalog", name=where))
    found, unread = [], []
    for n in [where] if where else sorted(mps):
        cat, why = marketplaces._read(state_dir, n, mps[n])
        if cat is None:
            if where:
                raise Error(f"{n}: {why}")
            unread.append(f"{n} — {why}")
            continue
        found += [(n, mps[n], cat, e) for e in cat["entries"] if e["name"] == name]
    if not found:
        if unread:
            raise Error(i18n.t("core.mpinstall.err_no_entry_unread", text=text, name=name, unread="; ".join(unread)))
        raise Error(i18n.t("core.mpinstall.err_no_entry", text=text, name=name))
    if len(found) > 1:
        raise Error(i18n.t("core.mpinstall.err_ambiguous", name=name, found=", ".join(f"{name}@{f[0]}" for f in found)))
    return found[0]


def _manifest(root):
    from modules.market import market
    try:
        with open(os.path.join(root, market.MANIFEST), encoding="utf-8") as f:
            return market.valid(json.load(f))
    except (OSError, ValueError):
        return None


def _promised(e, man, rights):
    """What the code asks for beyond the rights per the catalog's words (the entry's `rights`): a catalog may lie in the
    description, not in the rights. A process only with a named command: `process: true` promises "some process", but the
    machine will get `run`."""
    said = e.get("rights")
    if said is None:
        return []
    kinds = {k if k in ("page", "process", "themes") else "skills" for k, _ in rights}
    out = [plugrights.LABELS[k] for k in sorted(kinds) if not said.get(k)]
    run = " ".join(man["run"]) if man.get("run") else None
    if run and said.get("process") and said["process"] != run:
        out.append(i18n.t("core.mpinstall.promised_process", run=run, said=said["process"])
                   if isinstance(said["process"], str) else i18n.t("core.mpinstall.promised_unnamed", run=run))
    return out


def _unnamed(rights):
    """Executable things beyond the page, process, themes and skills (hooks, MCP, LSP, monitors, bin/...): the catalog's
    named `rights` do not cover them - "skills" does not mean "and a hook with a command"."""
    return [r for r in rights if r[0] not in ("page", "process", "themes", "skills")]


def _old_folder(state_dir, path):
    """A folder of the earlier storage (<state>/market/...) or None - it is removed once the new one is in place."""
    if not isinstance(path, str) or not path:
        return None
    old = os.path.realpath(os.path.join(state_dir, OLD_STORE))
    real = os.path.realpath(os.path.expanduser(path))
    return real if real.startswith(old + os.sep) and os.path.basename(real) != "_review" else None


def install(state_dir, cfg_path, text, accept=False, despite_revoked=False, via="cli", events=None, restart=None,
            preview=False, at=None):
    """Install a catalog entry or update it to the catalog's version -> lines. New rights without accept -
    NeedsConsent, nothing changes. preview - only the plugin's rights from the manifest at the catalog commit (as lines),
    before installing: the page shows them when the catalog did not name rights; hub.json is untouched. at - the catalog
    commit at which the owner looked at the entry (the page): the catalog has been updated since - a refusal, the rights
    must be seen again."""
    n, m, cat, e = find(state_dir, cfg_path, text)
    if at is not None and m.get("commit") != at:
        raise Error(i18n.t("core.mpinstall.err_catalog_moved", catalog=n, entry=e["name"]))
    label = f"{e['name']}@{n}"
    if e.get("revoked") and not despite_revoked:
        raise Error(i18n.t("core.mpinstall.err_revoked", label=label, why=e["revoked"]))
    if not e["pinned"] and e["type"] != "skillset":   # a skill set entry: the hub pins the current commit (_skillset)
        gh = github_of(e["source"].get("url")) if e["type"] != "module" else None
        if gh:
            raise Error(i18n.t("core.mpinstall.err_unpinned_review", label=label, gh=gh))
        raise Error(i18n.t("core.mpinstall.err_unpinned", label=label))
    official = bool(m.get("official")) and marketplaces.is_official(m.get("source"))
    if e["type"] == "module":
        if preview:
            raise Error(i18n.t("core.mpinstall.err_core_module", label=label))
        return _module(state_dir, cfg_path, n, m, e, accept, official)
    src = marketplace.source_of(m["source"], m["commit"], e)
    if e["type"] == "skillset":
        return _skillset(state_dir, cfg_path, n, e, src, label, accept, official, via, events, preview)
    url, sub = src["url"], src.get("subdir")
    store = marketplace.plugins_store(state_dir)
    try:
        commit, root = marketplace.fetch_code(store, src)
    except (RuntimeError, OSError) as err:
        raise Error(f"{label}: {err}") from None
    notes, wider = [], []

    def give_up(why):
        with store.busy():
            store.cleanup(catalog.read_settings(cfg_path), url)   # unpacked in vain - away with it (an installed one is held by references)
        raise why

    man = _manifest(root)
    if not man:
        give_up(Error(i18n.t("core.mpinstall.err_no_manifest", label=label,
                             place=f"{codestore.shown(url)} @ {commit[:12]}{'/' + sub if sub else ''}")))
    st, why = version.compat(man["hub"])
    if st == "incompatible":
        give_up(Error(f"{label}: {why}"))
    rights = plugrights.of(man, root)
    lied = _promised(e, man, rights)
    if preview:   # a preview does not refuse: the owner sees everything the code asks for, and the mismatch with the catalog too
        with store.busy():
            store.cleanup(catalog.read_settings(cfg_path), url)
        return [plugrights.shown(r) for r in rights] + (
            [i18n.t("core.mpinstall.preview_lied", lied="; ".join(lied))] if lied else [])
    if lied:
        give_up(Error(i18n.t("core.mpinstall.err_lied", label=label, lied="; ".join(lied))))
    cfg = catalog.read_settings(cfg_path)
    key, ext, sets = _records(cfg, url, sub, n, e["name"])
    if key is None:
        key = key_for(url, n, man["name"])   # for "./path" the url is the catalog's own address: the author is its owner
    if ext is None and not sets:   # first installation: executable things the catalog did not name are shown first
        wider = _unnamed(rights)
        if wider and not accept:
            give_up(NeedsConsent(_first_text(label, commit, wider), wider))
    else:
        wider = plugrights.wider(rights, plugrights.approved_of(ext, sets)[0])
        if wider and not accept:
            give_up(NeedsConsent(_consent_text(key, commit, wider, f"hub marketplace install {label}"), wider))
    if why:
        notes.append(why)
    if man["name"] != e["name"] or (e.get("version") and man["version"] and e["version"] != man["version"]):
        notes.append(i18n.t("core.mpinstall.note_mismatch", entry=f"{e['name']} {e.get('version') or ''}",
                            code=f"{man['name']} {man['version']}").rstrip())
    if not official:
        notes.append(i18n.t("core.mpinstall.third_party"))
    clash = _clash(cfg, key, url, sub, n, e["name"])
    if clash:   # before the hub.json lock - away with the downloaded (under the lock - the same, in case of a write in the same second)
        give_up(Error(clash))
    old = {}
    with catalog.edit_settings(cfg_path) as raw:
        exts = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
        k2, ext_now, sets_now = _records(raw, url, sub, n, e["name"])
        if k2 is not None and k2 != key:
            key = k2
        clash = _clash(raw, key, url, sub, n, e["name"])
        if clash:
            raise Error(clash)
        prev = ext_now or (sets_now[0] if sets_now else {})
        old = {"path": prev.get("path"), "commit": prev.get("commit"), "run": bool((ext_now or {}).get("run")),
               "version": prev.get("version"), "source": prev.get("source"), "subdir": prev.get("subdir")}
        off = bool((ext_now or {}).get("disabled"))
        if off and (ext_now or {}).get("revoked"):   # a revocation switched it off - the owner switches it on
            notes.append(i18n.t("core.mpinstall.note_revoked_off", why=ext_now["revoked"]))
        own = {k: v for k, v in (ext_now or {}).items() if k not in MANAGED}
        rec = {"path": root, "source": stored(url), **({"subdir": sub} if sub else {}), "commit": commit, "market": n,
               "entry": e["name"], "approved": rights, **({"revoked": e["revoked"]} if e.get("revoked") else {})}
        if man["web"] or man["run"] or man["themes"]:
            exts[key] = {**own, **rec, **{f: man[f] for f in FROM_MANIFEST if man[f]}, **({"disabled": True} if off else {})}
        else:
            exts.pop(key, None)
        raw["extensions"] = exts
        rest = [s for s in raw.get("sets") or [] if not _same(s, url, sub) and s not in sets_now] \
            if isinstance(raw.get("sets"), list) else []
        if man["skills"]:
            rest.append({"name": key, **rec})
        if man["skills"] or isinstance(raw.get("sets"), list):
            raw["sets"] = rest
    folder = _old_folder(state_dir, old["path"])
    if folder:   # the earlier storage: the new folder is already in hub.json - the old one is not needed (Windows: busy - it stays)
        try:
            codestore.remove(folder)
        except OSError:
            pass
    moved = isinstance(old["source"], str) and old["source"] and not marketplaces.same(url_of(old["source"]), url)
    with store.busy():
        try:
            store.cleanup(catalog.read_settings(cfg_path), url,
                          also=(old["commit"],) if old["commit"] and old["commit"] != commit else ())
            if moved:   # the code of the earlier source: nothing refers to it once its process is gone (hub-up.json)
                store.cleanup(catalog.read_settings(cfg_path), url_of(old["source"]))
        except OSError:
            pass
    if moved or (old["source"] and (old["subdir"] or None) != (sub or None)):
        notes.append(i18n.t("core.mpinstall.note_moved", old=_where(old["source"], old["subdir"]),
                            new=_where(stored(url), sub)))
    if man["web"]:
        notes.append(i18n.t("cli.market.note_page_access"))
    if man["themes"]:
        notes.append(i18n.t("cli.market.note_themes"))
    if man["skills"]:
        notes.append(i18n.t("cli.market.note_skills", name=key))
    if (man["run"] and not off) or old["run"]:   # the process is in the new folder; a removed process is stopped by hub-up
        if restart:
            restart([f"ext:{key}"])
            notes.append(i18n.t("cli.market.note_process_started" if man["run"] else "core.mpinstall.note_process_stopped",
                                name=key))
        else:
            notes.append(i18n.t("core.mpinstall.note_process_later", name=key))
    if wider:
        notes.append(i18n.t("cli.market.note_allowed", rights="; ".join(plugrights.shown(w) for w in wider)))
    if events:
        if wider:   # the consent is visible: from the page it could also be clicked by the plugin's code (section 3.6)
            events("plugins.consented", {"key": key, "sha": commit, "source": stored(url), "market": n, "via": via,
                                         "wider": [plugrights.shown(w) for w in wider]})
        events("plugins.changed", {"what": "install", "name": key})
    was = old["commit"] or old["path"]   # was installed (a skill set from before catalogs has no commit in its record - only a folder)
    # the installed one was newer than the catalog's pin (installed from the author's head) - this is a rollback, and we say so
    back = was and marketplaces.version_order(man["version"], old["version"]) == -1
    head = (i18n.t("core.mpinstall.rolled_back", name=key, version=man["version"], old=old["version"], label=label,
                   commit=commit[:12]) if back else
            i18n.t("core.mpinstall.updated" if was else "core.mpinstall.installed", name=key, label=label,
                   commit=commit[:12]))
    return [head] + [f"  {x}" for x in notes] + (
        [] if was else ["  " + i18n.t("core.mpinstall.rights", rights="; ".join(plugrights.shown(r) for r in rights))])



def _skillset(state_dir, cfg_path, n, e, src, label, accept, official, via, events, preview):
    """A skill set entry (a plugin of a Claude Code or Codex catalog) -> lines. Its folder only goes into the storage
    (marketplace.fetch_code name) and into `sets` as one plugin: {name: <catalog>, entry, plugin: true, path, source,
    subdir, commit, market, approved}; the session gets it as <entry>@<catalog> — the same id as in Claude Code and from
    `hub tools import set`, so what was granted keeps working. Nothing of it runs until it is granted to a session, so
    the first install asks no consent: the grant does, by what the plugin runs (catalog._runs, owner 2026-11-17). An
    update that asks more than the installed rights waits for --accept: granted sessions would run it at once. An entry
    the catalog does not pin is pinned here, at the current commit of its ref."""
    url, sub = src["url"], src.get("subdir")
    store = marketplace.plugins_store(state_dir)
    try:
        commit, root = marketplace.fetch_code(store, src, name=e["name"])
    except (RuntimeError, OSError) as err:
        raise Error(f"{label}: {err}") from None

    def give_up(why):
        with store.busy():
            store.cleanup(catalog.read_settings(cfg_path), url)
        raise why

    own = plugrights._plugin(root, e["name"], {})
    runs = plugrights.runs(root, e["name"], rights=own)
    # what runs is approved too (scripts sealed with their contents, apps — kinds for skill set entries only): an update
    # that adds or rewrites a script of a plugin granted to sessions would reach them without the owner
    rights = [list(r) for r in sorted({("skills", "")} | own | {tuple(r) for r in plugrights.runs(
        root, e["name"], sealed=True, rights=own)})]
    if preview:
        with store.busy():
            store.cleanup(catalog.read_settings(cfg_path), url)
        return [plugrights.shown(r) for r in rights]
    tool = f"{e['name']}@{n}"
    mine = lambda x: isinstance(x, dict) and x.get("plugin") and x.get("market") == n and x.get("entry") == e["name"]  # noqa: E731
    cfg = catalog.read_settings(cfg_path)
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    old = next((x for x in sets if mine(x)), None)
    other = next((x for x in sets if isinstance(x, dict) and x.get("name") == n and x.get("market")
                  and not (x.get("plugin") and x.get("market") == n)), None)
    if other:   # a whole set installed under this name from another catalog: the ids would mix
        give_up(Error(i18n.t("core.mpinstall.clash_set", name=n, source=other.get("source"))))
    wider = plugrights.wider(rights, old.get("approved") if isinstance((old or {}).get("approved"), list) else None) \
        if old else []
    if wider and not accept:
        give_up(NeedsConsent(_consent_text(tool, commit, wider, f"hub marketplace install {label}"), wider))
    rec = {"name": n, "entry": e["name"], "plugin": True, "path": root, "source": stored(url),
           **({"subdir": sub} if sub else {}), "commit": commit, "market": n, "approved": rights,
           **({"version": e["version"]} if e.get("version") else {}), **({"pinned_here": True} if not e["pinned"] else {})}
    with catalog.edit_settings(cfg_path) as raw:
        now = raw.get("sets") if isinstance(raw.get("sets"), list) else []
        # a set of the same name from `hub tools import set` stays: its other plugins are still in the catalog, and this
        # record comes after it, so <entry>@<catalog> is this one (catalog.build: the later set wins)
        raw["sets"] = [x for x in now if not mine(x)] + [rec]
    with store.busy():
        try:
            store.cleanup(catalog.read_settings(cfg_path), url,
                          also=(old["commit"],) if old and old.get("commit") and old["commit"] != commit else ())
        except OSError:
            pass
    if events:
        if wider:
            events("plugins.consented", {"key": tool, "sha": commit, "source": stored(url), "market": n, "via": via,
                                         "wider": [plugrights.shown(w) for w in wider]})
        events("plugins.changed", {"what": "install", "name": tool})
    head = i18n.t("core.mpinstall.updated" if old else "core.mpinstall.installed", name=tool, label=label,
                  commit=commit[:12])
    notes = [i18n.t("core.mpinstall.note_pinned_here")] if not e["pinned"] else []
    if not official:
        notes.append(i18n.t("core.mpinstall.third_party"))
    notes.append(i18n.t("core.mpinstall.note_runs", runs="; ".join(plugrights.shown(r) for r in runs)) if runs else
                 i18n.t("core.mpinstall.note_text_only"))
    notes.append(i18n.t("core.mpinstall.note_grant", tool=tool))
    return [head] + [f"  {x}" for x in notes]


def _module(state_dir, cfg_path, n, m, e, accept, official):
    """A module entry (after the revocation and sha checks): core/modsrc.from_catalog - no such module - a source exactly at
    the catalog's sha, already installed - an update to it; asks for more than approved without accept - NeedsConsent,
    hub.json as it was."""
    label, asked = f"{e['name']}@{n}", []
    try:
        lines, ok = modsrc.from_catalog(state_dir, cfg_path, marketplace.source_of(m["source"], m["commit"], e),
                                        market=n, entry=e["name"], accept=accept, asked=asked, rights=e.get("rights"))
    except (RuntimeError, OSError) as err:
        raise Error(f"{label}: {err}") from None
    if not ok:   # without accept the "asks for more" lines exist only for a refusal pending consent (a catalog's trusted does not apply)
        raise NeedsConsent("\n".join(lines), asked, shown=asked) if asked and not accept else Error("\n".join(lines))
    if e.get("revoked"):
        lines.append("  " + i18n.t("core.mpinstall.installed_despite_revoked", why=e["revoked"]))
    if not official:
        lines.append("  " + i18n.t("core.mpinstall.third_party"))
    return lines


def _consent_text(key, commit, wider, again):
    return i18n.t("cli.market.wider_consent", name=key, commit=commit[:12],
                  rights="; ".join(plugrights.shown(w) for w in wider), again=again)


def _first_text(label, commit, wider):
    """The refusal of a first install: the plugin holds executable things (a hook, an MCP server, a program in bin/...), and
    on the first install they always ask the owner's consent, whatever the catalog says; the way out is named."""
    return i18n.t("cli.market.consent_first", label=label, commit=commit[:12],
                  what="; ".join(plugrights.shown(w) for w in wider), button=i18n.t("web.plugins.install_with_rights"))


def _find_installed(cfg, name):
    """(key, source, subdir) of what was installed from a marketplace: the key, the short name (if unique) or entry@catalog."""
    exts = cfg.get("extensions") if isinstance(cfg.get("extensions"), dict) else {}
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    rows = [(k, x) for k, x in exts.items() if isinstance(x, dict) and x.get("source")]
    rows += [(marketplaces.set_key(s), s) for s in sets if isinstance(s, dict) and s.get("source") and isinstance(s.get("name"), str)]
    entry, _, where = str(name).partition("@")
    hits = {k: x for k, x in rows if k == name} or \
        ({k: x for k, x in rows if x.get("entry") == entry and x.get("market") == where} if where else
         {k: x for k, x in rows if k.split(":")[-1] == name or (x.get("plugin") and x.get("entry") == name)})
    if len(hits) > 1:
        raise Error(i18n.t("core.mpinstall.err_several", name=name, hits=", ".join(sorted(hits))))
    if not hits:
        mods = cfg.get("modules") if isinstance(cfg.get("modules"), dict) else {}
        if any(isinstance(s, dict) and s.get("entry") == entry and (not where or s.get("market") == where)
               for s in mods.get("sources") or []):   # a path, not a command: the source name and address are chosen by the owner
            raise Error(i18n.t("core.mpinstall.err_core_not_plugin", name=name))
        raise Error(i18n.t("core.mpinstall.err_not_installed", name=name))
    k, x = next(iter(hits.items()))
    return k, url_of(x["source"]), x.get("subdir") or None


def uninstall(state_dir, cfg_path, name, events=None, restart=None):
    """Remove what was installed (the records in extensions and sets of the same source) -> lines; the process is stopped by hub-up."""
    key, url, sub = _find_installed(catalog.read_settings(cfg_path), name)
    with catalog.edit_settings(cfg_path) as raw:
        exts = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
        gone = [exts.pop(k) for k in [k for k, x in exts.items() if _same(x, url, sub)]]
        sets = raw.get("sets") if isinstance(raw.get("sets"), list) else []
        gone += [s for s in sets if _same(s, url, sub)]
        if isinstance(raw.get("sets"), list):
            raw["sets"] = [s for s in sets if not _same(s, url, sub)]
    if any(x.get("run") for x in gone) and restart:
        restart([f"ext:{key}"])
    for x in gone:
        folder = _old_folder(state_dir, x.get("path"))
        if folder:
            platforms.current.rmtree(folder)
    store = marketplace.plugins_store(state_dir)
    if os.path.isdir(store.source_dir(url)):
        with store.busy():
            try:
                store.cleanup(catalog.read_settings(cfg_path), url)
            except OSError:
                pass
    if events:
        events("plugins.changed", {"what": "remove", "name": key})
    return [i18n.t("cli.market.removed", name=key)]


def revoke(cfg_path, name, rows, restart=None, notify=None, events=None):
    """The official catalog revoked entries: switch off what was installed from them (an extension - `disabled`, a skill
    set - removed from `sets`) and tell the owner the reason -> lines. A revocation the owner already knows about
    (`revoked` in the record - switched off earlier or installed despite it) is not switched off a second time."""
    lines, stop = [], []
    if not any(e.get("revoked") for e, *_ in rows):
        return lines   # no revocations - do not rewrite hub.json
    with catalog.edit_settings(cfg_path) as raw:
        exts = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
        sets = raw.get("sets") if isinstance(raw.get("sets"), list) else []
        for e, what, key, _ in rows:
            why = e.get("revoked")
            if not why:
                continue
            if what == "plugin" and isinstance(exts.get(key), dict) and exts[key].get("revoked") != why:
                exts[key].update(disabled=True, revoked=why)
                stop += [key] if exts[key].get("run") else []
                lines.append(i18n.t("core.mpinstall.revoke_off", name=key, catalog=name, entry=e["name"], why=why))
            elif what == "skillset" and any(marketplaces.set_key(s) == key and s.get("revoked") != why for s in sets
                                                  if isinstance(s, dict)):
                raw["sets"] = sets = [s for s in sets if not (isinstance(s, dict) and marketplaces.set_key(s) == key)]
                lines.append(i18n.t("core.mpinstall.revoke_set", name=key, catalog=name, entry=e["name"], why=why))
    if stop and restart:
        restart([f"ext:{k}" for k in stop])
    if lines and events:   # open pages reload without the switched-off page code
        events("plugins.changed", {"what": "revoke", "name": name})
    if lines and notify:
        notify(i18n.t("web.settings.tab.plugins"), "\n".join(x.lstrip("✗ ") for x in lines))
    return lines
