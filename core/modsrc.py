"""Core modules from git (Module API, spec appendices A.2 and B): `hub module add <url>` and `hub module update`.

hub.json: `"modules": {"sources": [{"git": "<url>", "ref": "v1.2", "subdir": "x", "commit": "<sha>", "pinned": true}]}`
— the source is pinned to a commit the owner approved; its modules are put into `modules.enabled` when added.

On disk, `<state>/modules/<repo>-<address hash>/` (the storage is core/codestore.py, shared with marketplace plugins):
- `repo.git` — a bare clone (the source's branches and tags), git with the machine's keys, without password prompts;
- `<commit>/` — the files of this commit (`git archive`), the module process works from it; `<commit>.files.json` —
  the list of its files with sizes and hashes, written last: a folder without it does not exist. An update prepares a
  new folder, hubd sees the new commit in hub.json, and hub-up restarts the process already in it — the old process
  reads only its own files until it stops, versions do not mix. The previous folder is deleted at the next update
  (unless a want file holds it).
- Editing a commit file in the folder by hand (the same commit, other code) makes the module blocked, `hub module
  update` restores the files. New files in the folder (cache, Python bytecode) are not checked: whoever writes into
  the state folder has the hub's rights anyway.

`hub module update` applies a new commit only if it does not ask for more than what was approved (`wider`): new
rights, modules, dependencies and plugins, changed ranges, environment names, another program, new manifest fields
(points of later API versions); otherwise only with `--accept`, and until then the module works on the previous
commit. A source marked `"trusted": true` (`hub module trust`) goes without `--accept`: what the new commit asks for
beyond is printed.

A module from a catalog (`hub marketplace install <entry>`, from_catalog) is a source with `"market"` and `"entry"`
(where from and which entry), pinned to the catalog's commit (`ref` = `commit` = sha). A new version is a new sha in
the catalog and the same `install`: the update path to that sha, with the same wider and `--accept`; `trusted` has no
effect on such a source.

A module from disk — `hub module add <folder>`: the folder (with module.json or with module folders) goes into
`modules.paths`, the code runs straight from it, without a commit or version approval; `--dev` and `hub module dev
<name>` — developer mode (`modules.dev`, core/modrun.py: a file edit means a restart). All commands need the owner's
word (bin/hub). Module code is not run here.
"""
import contextlib
import json
import os
import re

from core import catalog, codestore, i18n, marketplace, modules

DIR = codestore.MODULES
REPO_DIR = codestore.REPO_DIR
LISTING = codestore.LISTING
COMMIT = codestore.COMMIT
# manifest fields whose change does not widen rights; everything else is a widening (also fields of future points)
SAME = {"version", "description", "api", "budget"}
# lists where a widening is only new elements (order and removed ones are not a widening)
SETS = {("capabilities",), ("env",), ("provides",), ("requires", "plugins")}
shown = codestore.shown


def _store(state_dir):
    """The module code storage (core/codestore.py): root <state>/modules/, without limits and without strict checking
    of extra files — as it was before the shared layer."""
    return codestore.Store(state_dir, DIR, fix="hub module update")


def source_dir(state_dir, url):
    """The source folder: the repo name for a human and the address hash (two repos with one name)."""
    return _store(state_dir).source_dir(url)


def git_dir(state_dir, url):
    return _store(state_dir).git_dir(url)


def commit_dir(state_dir, url, commit):
    return _store(state_dir).commit_dir(url, commit)


def _subdir_ok(subdir):
    return subdir is None or (isinstance(subdir, str) and subdir.strip() and not os.path.isabs(subdir)
                              and ".." not in subdir.replace("\\", "/").split("/") and not re.match(r"^[A-Za-z]:", subdir))


def fetch(state_dir, url, ref=None):
    """The source into a bare clone -> (commit, pinned) for ref: a branch moves (pinned False), a tag and a commit do
    not. An error is a RuntimeError with git's words (core/codestore.fetch_into)."""
    return _store(state_dir).fetch(url, ref)


def _listing_path(state_dir, url, commit):
    return _store(state_dir).listing_path(url, commit)


def damage(state_dir, url, commit):
    """None — the commit folder is intact; otherwise what is wrong with it: not unpacked or files changed."""
    return _store(state_dir).damage(url, commit)


def unpack(state_dir, url, commit):
    """The commit folder from the bare clone (git archive), if it is absent or changed; -> the path."""
    return _store(state_dir).unpack(url, commit)


_remove, _drop = codestore.remove, codestore.drop


def _modules_in(folder, subdir):
    """{name: found module} in the commit folder (a module folder or a folder of folders)."""
    root = os.path.join(folder, *subdir.replace("\\", "/").split("/")) if subdir else folder
    return {f["name"]: f for f in modules._scan(root, "git") if os.path.isfile(os.path.join(f["dir"], "module.json"))}


def discover(cfg, state_dir):
    """Modules of the sources from hub.json — from the folders of their approved commits. Without sources — nothing
    and no disk access at all."""
    out = []
    for src in modules.settings(cfg).get("sources") or []:
        url, commit, sub = str(src.get("git") or ""), str(src.get("commit") or ""), src.get("subdir")
        extra = {"source": url, "ref": src.get("ref"), "commit": commit, "pinned": src.get("pinned", True),
                 **({"trusted": True} if src.get("trusted") is True and not src.get("market") else {})}
        name = (sub or "").replace("\\", "/").rstrip("/").split("/")[-1] or os.path.basename(source_dir(state_dir, url))
        if not url or not COMMIT.fullmatch(commit) or not _subdir_ok(sub):
            out.append({"name": name, "version": "?", "dir": "", "origin": "git", "manifest": {}, **extra,
                        "errors": [i18n.t("core.modsrc.source_broken")]})
            continue
        why = damage(state_dir, url, commit)
        folder = commit_dir(state_dir, url, commit)
        if why and not os.path.exists(_listing_path(state_dir, url, commit)):
            out.append({"name": name, "version": "?", "dir": folder, "origin": "git", "manifest": {}, **extra,
                        "errors": [why]})
            continue
        root = os.path.join(folder, *sub.replace("\\", "/").split("/")) if sub else folder
        items = modules._scan(root, "git", **extra)
        for it in items:
            if why:   # code not approved: only what the owner saw in hub module add/update
                it["errors"] = it["errors"] + [why]
        out += items
    return out


# --- what a new commit asks beyond the approved ---------------------------------------------------------------------

def _more(was, now, path=()):
    out = []
    for key in sorted(now, key=str):
        if not path and key in SAME:
            continue
        p, a, b = path + (key,), (was or {}).get(key) if isinstance(was, dict) else None, now[key]
        where = ".".join(map(str, p))
        if p in SETS and isinstance(b, list):
            new = [x for x in b if x not in (a if isinstance(a, list) else [])]
            if new:
                out.append(f"{where}: + {', '.join(map(str, new))}")
        elif isinstance(a, dict) and isinstance(b, dict):
            out += _more(a, b, p)
        elif a != b:
            show = lambda v: json.dumps(v, ensure_ascii=False)[:120]   # noqa: E731
            out.append(f"{where}: {show(b)}" if a is None else f"{where}: {show(a)} → {show(b)}")
    return out


def wider(old, new):
    """Lines for the owner: what the modules of the new commit (new) ask beyond the approved ones (old); [] — none."""
    out = []
    for name, f in sorted(new.items()):
        if name not in old:
            out.append(i18n.t("core.modsrc.new_module", what=describe(f)))
            continue
        more = _more(old[name]["manifest"], f["manifest"])
        if more:
            out.append(i18n.t("core.modsrc.asks_more", name=name, version=f["version"], more="; ".join(more)))
    return out


def describe(f):
    """What the owner looks at before a module starts: version, rights, dependencies, program."""
    man = f["manifest"]
    req = man.get("requires") or {}
    parts = [i18n.t("core.modsrc.d_rights",
                    list=", ".join(map(str, man.get("capabilities") or [])) or i18n.t("core.modsrc.none"))]
    if req.get("modules"):
        parts.append(i18n.t("core.modsrc.d_modules", list=", ".join(f"{n} {r}" for n, r in req["modules"].items())))
    if req.get("plugins"):
        parts.append(i18n.t("core.modsrc.d_plugins", list=", ".join(map(str, req["plugins"]))))
    if man.get("env"):
        parts.append(i18n.t("core.modsrc.d_env", list=", ".join(map(str, man["env"]))))
    if isinstance(man.get("run"), list):
        parts.append(i18n.t("core.modsrc.d_run", list=" ".join(map(str, man["run"]))[:200]))
    elif modules.processless(man):
        parts.append(i18n.t("core.modsrc.d_static"))
    if f.get("errors"):
        parts.append(i18n.t("core.modsrc.d_errors", list="; ".join(f["errors"])))
    return i18n.t("core.modsrc.describe", name=f["name"], version=f["version"], parts="; ".join(parts))


# --- commands -------------------------------------------------------------------------------------------------------

def _busy(state_dir):
    """One command over sources at a time: two do not download or delete each other's folders."""
    return _store(state_dir).busy()


def add_source(cfg_path, src, names):
    """Into hub.json under a lock: the source (the same address and subdir — replace) and its modules go to enabled."""
    with catalog.edit_settings(cfg_path) as raw:
        mods = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
        raw["modules"] = mods
        mods["sources"] = [s for s in modules.settings(raw).get("sources") or []
                           if (s.get("git"), _sub(s.get("subdir"))) != (src["git"], _sub(src.get("subdir")))] + [src]
        mods["enabled"] = list(dict.fromkeys(list(modules.settings(raw).get("enabled") or []) + list(names)))


def _running(state_dir, base):
    """Commit folders in base in which a module process still runs (the want file)."""
    return _store(state_dir).running(base)


def _sweep(state_dir, cfg_path):
    """Source folders that are no longer in hub.json (removed by `hub module remove` while a process worked in the
    folder): everything the process no longer holds goes from disk. Called by commands over sources under the _busy
    lock, after hub.json is written. Failure to remove is not a command failure: -> lines about what is left."""
    root = os.path.join(state_dir, DIR)
    if not os.path.isdir(root):
        return []
    try:
        cfg = catalog.read_settings(cfg_path)
    except ValueError:
        return []
    section = cfg.get("modules", {})
    raw = section.get("sources", []) if isinstance(section, dict) else None
    if not (isinstance(raw, list) and all(isinstance(s, dict) and isinstance(s.get("git", ""), str) for s in raw)):
        return []   # a section with a typo reads as empty: it cannot decide what is extra
    live = {os.path.basename(source_dir(state_dir, s["git"])) for s in raw if s.get("git")}
    left = []
    try:
        names = os.listdir(root)
    except OSError as e:
        return [f"{root}: {e}"]
    for n in names:
        base = os.path.join(root, n)
        try:
            if n in live or not os.path.isdir(base):
                continue
            entries = os.listdir(base)
            if not entries or any(e != REPO_DIR and not COMMIT.fullmatch(e.split(".")[0]) for e in entries):
                continue   # an empty folder or not ours
            busy = _running(state_dir, base)
            for e in entries:
                if e.split(".")[0] in busy:   # the process's <commit> and <commit>.files.json
                    continue
                path = os.path.join(base, e)
                try:
                    _remove(path) if os.path.isdir(path) else _drop(path)
                except OSError as err:
                    left.append(f"{path}: {err}")
            try:
                os.rmdir(base)
            except OSError:
                pass
        except OSError as e:
            left.append(f"{base}: {e}")
    return left


def _cleanup(state_dir, cfg_path, url, also=()):
    """Commit folders of the source that are no longer needed: not in hub.json, not in a want file (a process may still
    work in it) and not just replaced (also); and abandoned unfinished unpackings. The references are everything the
    storage knows (codestore.references: plugin folders and skill set folders too)."""
    try:
        cfg = catalog.read_settings(cfg_path)
    except ValueError:
        return
    _store(state_dir).cleanup(cfg, url, also)


def add(state_dir, cfg_path, url, ref=None, subdir=None):
    """`hub module add`: the clone, pinning the commit, its folder, modules go to enabled. -> lines for the owner."""
    if not _subdir_ok(subdir):
        raise RuntimeError(i18n.t("core.modsrc.subdir_bad"))
    subdir = _sub(subdir)   # "a/b/", "a\\b" — as a catalog entry writes it: "a/b"
    with _busy(state_dir):
        commit, pinned = fetch(state_dir, url, ref)
        found = _modules_in(unpack(state_dir, url, commit), subdir)
        if not found:
            _cleanup(state_dir, cfg_path, url)
            raise RuntimeError(i18n.t("core.modsrc.no_module_json",
                                      where=shown(url) + (f" / {subdir}" if subdir else "")))
        add_source(cfg_path, {"git": url, **({"ref": ref} if ref else {}), **({"subdir": subdir} if subdir else {}),
                              "commit": commit, "pinned": pinned}, sorted(found))
        _cleanup(state_dir, cfg_path, url)
        _sweep(state_dir, cfg_path)
    return ([i18n.t("core.modsrc.added", url=shown(url), commit=commit[:12]) if pinned else
             i18n.t("core.modsrc.added_moving", url=shown(url), commit=commit[:12],
                    ref=ref or i18n.t("core.modsrc.default_branch"))]
            + [describe(f) for _, f in sorted(found.items())]
            + [i18n.t("core.modsrc.enabled")])


def update(state_dir, cfg_path, which=None, accept=False):
    """`hub module update [name|address] [--accept]` -> (lines, ok). A new commit that widens rights waits for
    --accept, the module stays on the approved one; a source that cannot be downloaded works from its folder."""
    sources = [s for s in modules.settings(catalog.read_settings(cfg_path)).get("sources") or []
               if isinstance(s.get("git"), str) and s["git"]]
    if not sources:
        with _busy(state_dir) if os.path.isdir(os.path.join(state_dir, DIR)) else contextlib.nullcontext():
            _sweep(state_dir, cfg_path)
        return ([i18n.t("core.modsrc.no_git_modules")], True) if not which else (
            [i18n.t("core.modsrc.no_git_module", which=which)], False)
    with _busy(state_dir):
        return _move(state_dir, cfg_path, sources, which, accept)


def _move(state_dir, cfg_path, sources, which=None, accept=False, target=None, asked=None):
    """The body of update under the _busy lock: the sources go to a new commit (the source's ref) or, with target (a
    catalog entry, from_catalog: {sha, market, entry}), exactly to sha — then the trusted mark has no effect. -> (lines,
    ok). asked (a list) receives the "asks for more" lines — both those not applied without accept and accepted ones."""
    lines, ok, moved, enable, replaced = [], True, {}, [], {}
    for src in sources:
        url, sub, commit = src["git"], src.get("subdir"), str(src.get("commit") or "")
        approved = COMMIT.fullmatch(commit) and _subdir_ok(sub)
        old = _modules_in(commit_dir(state_dir, url, commit), sub) if approved and damage(
            state_dir, url, commit) is None else {}
        if which and which != url and which not in old and which not in _names(src, state_dir):
            continue
        # from a catalog (and a source installed from it) — without trusted: new rights from there always need consent
        trusted = src.get("trusted") is True and not src.get("market") and not target
        try:
            if target:
                from core import marketplace
                new_commit, pinned = marketplace.pin(_store(state_dir), url, target["sha"]), True
            else:
                new_commit, pinned = fetch(state_dir, url, src.get("ref"))
            fixed = False
            if approved and damage(state_dir, url, commit) is not None:
                try:   # the approved commit's folder is changed or gone — restore it: this is not new code
                    try:
                        old = _modules_in(unpack(state_dir, url, commit), sub)
                    except RuntimeError:   # only the branch's new commit is in the clone: fetch the old one by name
                        fetch(state_dir, url, commit)
                        old = _modules_in(unpack(state_dir, url, commit), sub)
                    fixed = True
                except RuntimeError:
                    if new_commit == commit:
                        raise
                    old = {}   # the approved commit is not in the source: everything in the new one is a widening
            mark = {"ref": new_commit, "market": target["market"], "entry": target["entry"]} if target else {}
            if new_commit == commit:
                lied = promised(old, target.get("rights")) if target else []
                if lied:   # the same code becomes "from a catalog": the catalog promises no less than it asks here too
                    lines.append(i18n.t("core.modsrc.promises_less_stands", url=shown(url), lied="; ".join(lied)))
                    ok = False
                    continue
                lines.append(i18n.t("core.modsrc.already_on_fixed" if fixed else "core.modsrc.already_on",
                                    url=shown(url), commit=commit[:12]))
                if mark and any(src.get(k) != v for k, v in mark.items()):   # the same code — now from a catalog
                    moved[(url, sub)] = {"pinned": True, **mark}
                    if (src.get("market"), src.get("entry")) != (target["market"], target["entry"]):
                        lines.append(_from_catalog_now(src, target))
                continue
            new = _modules_in(unpack(state_dir, url, new_commit), sub)
            lied = promised(new, target.get("rights")) if target else []
            if lied:   # a catalog may lie in the description, not in the rights — and --accept does not change that
                lines.append(i18n.t("core.modsrc.promises_less", url=shown(url), commit=new_commit[:12],
                                    lied="; ".join(lied)))
                ok = False
                continue
            more = wider(old, new)
            if asked is not None:
                asked += more
            if more and not accept and not trusted:
                again = (f"hub marketplace install {target['entry']}@{target['market']}" if target else
                         f"hub module update {which or (sorted(new)[0] if new else shown(url))}")
                lines += ([i18n.t("core.modsrc.not_applied", url=shown(url), commit=new_commit[:12])]
                          + [f"  {w}" for w in more] + ["  " + i18n.t("core.modsrc.accept_with", command=again)])
                ok = False
                continue
        except RuntimeError as e:
            lines.append(i18n.t("core.modsrc.update_failed", url=shown(url), error=e))
            ok = False
            continue
        moved[(url, sub)] = {"commit": new_commit, "pinned": pinned, **mark}
        replaced.setdefault(url, set()).add(commit)
        enable += [n for n in sorted(new) if n not in old]
        back = COMMIT.fullmatch(commit) and codestore.older(_store(state_dir).git_dir(url), new_commit, commit)
        lines.append(i18n.t(("core.modsrc.moved_back_catalog" if target else "core.modsrc.moved_back_source") if back
                            else "core.modsrc.moved_now", url=shown(url), commit=new_commit[:12], old=commit[:12])
                     + ("" if new else i18n.t("core.modsrc.tail_no_modules"))
                     + (i18n.t("core.modsrc.tail_trusted") if more and not accept else ""))
        lines += [f"  {w}" for w in more]
        if target and (src.get("market"), src.get("entry")) != (target["market"], target["entry"]):
            lines.append(_from_catalog_now(src, target))
    if moved:
        # git may have run for minutes: hub.json is re-read under the lock, only the moved sources are changed
        with catalog.edit_settings(cfg_path) as fresh:
            for s in modules.settings(fresh).get("sources") or []:
                if (s.get("git"), s.get("subdir")) in moved:
                    s.update(moved[(s.get("git"), s.get("subdir"))])
                    if s.get("market"):   # from a catalog — without trusted: new rights from there always need consent
                        s.pop("trusted", None)
            if enable:
                mods = fresh["modules"]
                mods["enabled"] = list(dict.fromkeys(list(modules.settings(fresh).get("enabled") or []) + enable))
    for url in {s["git"] for s in sources}:
        _cleanup(state_dir, cfg_path, url, replaced.get(url, ()))
    _sweep(state_dir, cfg_path)
    if not lines:
        lines, ok = [i18n.t("core.modsrc.no_git_module", which=which)], False
    return lines, ok


def _from_catalog_now(src, target):
    was = i18n.t("core.modsrc.was_catalog", market=src["market"]) if src.get("market") else (
        i18n.t("core.modsrc.was_ref", ref=src["ref"]) if src.get("ref") else i18n.t("core.modsrc.was_default"))
    return "  " + i18n.t("core.modsrc.from_catalog_now", market=target["market"], entry=target["entry"], was=was)


def _sub(s):
    """subdir for comparison: "a/b/", "a\\b" and "a/b" are one folder; the root is None."""
    return str(s or "").replace("\\", "/").strip("/") or None


RIGHTS = marketplace.MODULE_RIGHTS   # `rights` of a catalog module entry: lists of strings


def promised(found, said):
    """What the commit's modules (found) ask beyond the rights in the catalog's words (`rights` of a module entry:
    `capabilities`, `env` — lists; no key — nothing promised) -> lines; no rights — the catalog did not name any,
    nothing to compare."""
    if not isinstance(said, dict):
        return []
    out = []
    for key, label in RIGHTS.items():
        want = sorted({str(x) for f in found.values() if isinstance(f["manifest"].get(key), list)
                       for x in f["manifest"][key]})
        more = [x for x in want if x not in (said.get(key) if isinstance(said.get(key), list) else [])]
        if more:
            out.append(f"{label}: {', '.join(more)}")
    return out


def from_catalog(state_dir, cfg_path, src, market, entry, accept=False, asked=None, rights=None):
    """A catalog entry of `type: module` (`hub marketplace install`) -> (lines, ok). src is its source {url, sha,
    subdir?} (marketplace.source_of). If there is no module from this source (address and subdir) — add exactly at sha:
    `ref` and `commit` are sha, `pinned`, plus `market` and `entry`. If it is already installed (from a catalog or `hub
    module add`) — not add (it would replace the source bypassing wider) but the update path to sha: if it asks for
    more than approved, it is not applied without accept and hub.json stays as it was. rights — the entry's rights in
    the catalog's words: if the code asks for more — a refusal, even with accept. Not downloaded, no module.json or the
    catalog promises less on adding — RuntimeError. asked — as in _move."""
    from core import marketplace, marketplaces
    url, sha, sub = src["url"], src.get("sha"), src.get("subdir")
    if not (isinstance(sha, str) and COMMIT.fullmatch(sha)):
        raise RuntimeError(i18n.t("core.modsrc.entry_not_pinned", entry=entry, market=market))
    if not _subdir_ok(sub):
        raise RuntimeError(i18n.t("core.modsrc.subdir_bad"))
    with _busy(state_dir):
        cfg = catalog.read_settings(cfg_path)
        have = next((s for s in modules.settings(cfg).get("sources") or []
                     if isinstance(s.get("git"), str) and marketplaces.same(s["git"], url)
                     and _sub(s.get("subdir")) == _sub(sub)), None)
        if have is not None:   # under the same lock: the "update, not add" decision does not go stale between steps
            lines, ok = _move(state_dir, cfg_path, [have], accept=accept,
                              target={"sha": sha, "market": market, "entry": entry, "rights": rights}, asked=asked)
            mods = modules.settings(catalog.read_settings(cfg_path))
            now = next((str(s.get("commit") or "") for s in mods.get("sources") or []
                        if (s.get("git"), s.get("subdir")) == (have["git"], have.get("subdir"))), "")
            names = _modules_in(commit_dir(state_dir, have["git"], now), have.get("subdir")) if COMMIT.fullmatch(now) \
                and _subdir_ok(have.get("subdir")) else {}
            off = sorted(n for n in names if n not in set(mods.get("enabled") or []))
            return lines + (["  " + i18n.t("core.modsrc.disabled_hint", names=", ".join(off))] if ok and off else []), ok
        try:
            marketplace.pin(_store(state_dir), url, sha)
            found = _modules_in(unpack(state_dir, url, sha), sub)
            if not found:
                raise RuntimeError(i18n.t("core.modsrc.no_module_json",
                                          where=shown(url) + (f" / {sub}" if sub else "") + f" @ {sha[:12]}"))
            lied = promised(found, rights)
            if lied:
                raise RuntimeError(i18n.t("core.modsrc.promises_less_refuse", entry=entry, market=market,
                                          lied="; ".join(lied)))
        except (RuntimeError, OSError):
            _sweep(state_dir, cfg_path)   # the clone and folder just downloaded did not get a source in hub.json
            raise
        add_source(cfg_path, {"git": url, "ref": sha, **({"subdir": sub} if sub else {}), "commit": sha,
                              "pinned": True, "market": market, "entry": entry}, sorted(found))
        _cleanup(state_dir, cfg_path, url)
        _sweep(state_dir, cfg_path)
        return ([i18n.t("core.modsrc.added_from_catalog", entry=entry, market=market,
                        where=shown(url) + (f" / {sub}" if sub else ""), sha=sha[:12])]
                + [describe(f) for _, f in sorted(found.items())]
                + [i18n.t("core.modsrc.enabled")]), True


def _names(src, state_dir):
    """Module names of the source in any of its downloaded folders (the approved commit's folder is gone)."""
    base = source_dir(state_dir, src["git"])
    out = set()
    for n in os.listdir(base) if os.path.isdir(base) else []:
        if COMMIT.fullmatch(n):
            out |= set(_modules_in(os.path.join(base, n), src.get("subdir") if _subdir_ok(src.get("subdir")) else None))
    return out


# --- remove a module -------------------------------------------------------------------------------------------------

def _source_names(state_dir, src):
    """Module names of the source: from any of its downloaded folders; if there is none — the subdir folder name (that
    is how discover names them while the source is not downloaded)."""
    names = _names(src, state_dir)
    if names:
        return names
    sub = src.get("subdir")
    return {sub.replace("\\", "/").rstrip("/").split("/")[-1]} if isinstance(sub, str) and _subdir_ok(sub) else set()


def remove(state_dir, cfg_path, which):
    """`hub module remove <name|address>`: the module's source (a git repo with its subdir) or folder (modules.paths)
    goes from hub.json, the name from enabled and from consents (dev, model, core_write: the next module with this name
    does not inherit them); the source's downloaded files go from disk, except a folder in which a process still works
    (hub-up stops it per hub.json). A git repo address removes all its sources. -> lines for the owner; no such module
    — RuntimeError."""
    gone, base = [], []
    with _busy(state_dir):
        with catalog.edit_settings(cfg_path) as raw:
            st = modules.settings(raw)
            sources, paths = st.get("sources") or [], st.get("paths") or []
            hit_s = [(s, s["git"], _source_names(state_dir, s)) for s in sources
                     if isinstance(s.get("git"), str) and s["git"]]
            hit_s = [h for h in hit_s if which == h[1] or which in h[2]]
            hit_p = [(p, p, {f["name"] for f in modules._scan(os.path.expanduser(p), "path")}) for p in paths]
            hit_p = [h for h in hit_p if which in h[2] or _same_path(which, h[1])]
            for _, address, group in hit_s + hit_p:   # a name of one of several modules of a source does not remove the rest
                if which != address and not _same_path(which, address) and len(group) > 1:
                    raise RuntimeError(i18n.t("core.modsrc.one_of_many", which=which, address=shown(address),
                                              names=", ".join(sorted(group))))
            if not (hit_s or hit_p):
                if which in {f["name"] for f in modules.discover({}, state_dir)}:
                    raise RuntimeError(i18n.t("core.modsrc.builtin_module", which=which))
                raise RuntimeError(i18n.t("core.modsrc.no_such_module", which=which))
            names = set()
            for s, _, group in hit_s:
                names |= group
                gone.append(f"{shown(s['git'])}" + (f" / {s['subdir']}" if s.get("subdir") else ""))
            for p, _, group in hit_p:
                names |= group
                gone.append(str(p))
            others = [_source_names(state_dir, s) for s in sources if all(s is not h[0] for h in hit_s)
                      if isinstance(s.get("git"), str) and s["git"]]
            others += [{f["name"] for f in modules._scan(os.path.expanduser(p), "path")} for p in paths
                       if p not in [h[0] for h in hit_p]]
            drop = names - set().union(*others)   # another source gives the same name — its enabling and consents stay
            mods = raw["modules"]
            if hit_s:
                mods["sources"] = [s for s in sources if all(s is not h[0] for h in hit_s)]
            if hit_p:
                mods["paths"] = [p for p in paths if p not in [h[0] for h in hit_p]]
            for key in ("enabled", "dev", "model", "core_write"):
                if key in mods:
                    mods[key] = [n for n in st.get(key) or [] if n not in drop]
            base = sorted({h[1] for h in hit_s})
        late = []
        try:
            for url in base:
                _cleanup(state_dir, cfg_path, url)
        except OSError as e:   # hub.json is written: files that could not be removed go with the next command
            late.append(i18n.t("core.modsrc.files_left", error=e))
        late += [i18n.t("core.modsrc.left_on_disk", path=x) for x in _sweep(state_dir, cfg_path)]
    return ([i18n.t("core.modsrc.removed", what=", ".join(gone)),
             i18n.t("core.modsrc.modules_off", names=", ".join(sorted(names)))]
            + late)


# --- the "I trust it" source -----------------------------------------------------------------------------------------

def trust(state_dir, cfg_path, which, on=True):
    """`hub module trust <address|name> [--off]`: the "trusted" mark on a source — update applies new commits without
    --accept. -> lines for the owner; no such source — RuntimeError."""
    with catalog.edit_settings(cfg_path) as raw:
        hit = [s for s in modules.settings(raw).get("sources") or [] if isinstance(s.get("git"), str) and s["git"]
               and (which == s["git"] or which in _names(s, state_dir))]
        if not hit:
            raise RuntimeError(i18n.t("core.modsrc.no_git_module_hint", which=which))
        for s in hit:
            if on and s.get("market"):   # from a catalog — without trusted: new rights from there always need consent
                continue
            if on:
                s["trusted"] = True
            else:
                s.pop("trusted", None)
    word = i18n.t("core.modsrc.trust_on") if on else i18n.t("core.modsrc.trust_off")
    market = i18n.t("core.modsrc.trust_market")
    return [f"{shown(s['git'])}" + (f" / {s['subdir']}" if s.get("subdir") else "") + ": "
            + (market if on and s.get("market") else word) for s in hit]


# --- a module from disk ----------------------------------------------------------------------------------------------

def is_folder(arg):
    """The `hub module add` argument is a folder on disk (not a bare git repo: that is cloned, like an address)."""
    p = os.path.abspath(os.path.expanduser(str(arg)))
    return os.path.isdir(p) and not (os.path.isfile(os.path.join(p, "HEAD")) and os.path.isdir(os.path.join(p, "objects")))


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(a)))) == os.path.normcase(os.path.abspath(b))


def add_path(state_dir, cfg_path, folder, dev=False):
    """`hub module add <folder> [--dev]`: the folder goes into modules.paths, its modules into enabled (dev — also
    into modules.dev). -> lines for the owner."""
    path = os.path.abspath(os.path.expanduser(folder))
    found = {f["name"]: f for f in modules._scan(path, "path")}
    if not found:
        raise RuntimeError(i18n.t("core.modsrc.no_module_json_path", path=path))
    with catalog.edit_settings(cfg_path) as raw:
        mods = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
        raw["modules"] = mods
        have = list(modules.settings(raw).get("paths") or [])
        mods["paths"] = have + ([] if any(_same_path(p, path) for p in have) else [path])
        mods["enabled"] = list(dict.fromkeys(list(modules.settings(raw).get("enabled") or []) + sorted(found)))
        if dev:
            mods["dev"] = list(dict.fromkeys(list(modules.settings(raw).get("dev") or []) + sorted(found)))
    return ([i18n.t("core.modsrc.added_folder", path=path)]
            + [describe(f) for _, f in sorted(found.items())]
            + [i18n.t("core.modsrc.enabled_dev" if dev else "core.modsrc.enabled")])


class Unknown(RuntimeError):
    """There is no such module or it does not ask for this right: nothing to allow."""


def set_model(state_dir, cfg_path, name, on=True, changed=None):
    """`hub module model <name> [--off]`: a model call through the core on the hub's accounts (modules.model,
    core/modelcall.py) for a module, or not. Allowing is only for a found module with the model right (otherwise
    Unknown). changed — a list: (was, now) read under the same hub.json lock is put into it. -> lines."""
    from core import modelcall
    have = list(modules.settings(catalog.load_config(state_dir)).get("model") or [])
    if not on and name not in have:   # nothing to forbid — do not touch hub.json
        return [i18n.t("core.modsrc.model_already_off", name=name)]
    with catalog.edit_settings(cfg_path) as raw:
        if on and not modelcall.has_right(raw, state_dir, name):
            raise Unknown(i18n.t("core.modsrc.model_no_right", name=name))
        mods = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
        raw["modules"] = mods
        have = list(modules.settings(raw).get("model") or [])
        mods["model"] = list(dict.fromkeys(have + [name])) if on else [n for n in have if n != name]
        if changed is not None:
            changed.append((have, list(mods["model"])))
    return [i18n.t("core.modsrc.model_on" if on else "core.modsrc.model_off", name=name)]


def set_dev(state_dir, cfg_path, name, on=True):
    """`hub module dev <name> [--off]`: developer mode of a module from a folder (modules.dev) or of an embed's module
    (its folder in embed/<name>/modules). -> lines."""
    with catalog.edit_settings(cfg_path) as raw:
        if on:
            found = modules.discover(raw, state_dir)
            embedded = any(f["name"] == name and f.get("embed") for f in found)
            origin = {f["name"]: f["origin"] for f in found}.get(name)
            if origin != "path" and not embedded:
                raise RuntimeError(i18n.t("core.modsrc.dev_builtin" if origin == "builtin" else "core.modsrc.dev_git"
                                          if origin == "git" else "core.modsrc.dev_missing", name=name))
        mods = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
        raw["modules"] = mods
        have = list(modules.settings(raw).get("dev") or [])
        mods["dev"] = list(dict.fromkeys(have + [name])) if on else [n for n in have if n != name]
    from core import modrun
    return [i18n.t("core.modsrc.dev_on", name=name, budget=f"{modrun.DEV_BUDGET:g}") if on
            else i18n.t("core.modsrc.dev_off", name=name)]
