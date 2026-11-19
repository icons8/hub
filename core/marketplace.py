"""Marketplaces of plugins and modules (as in Claude Code): a catalog is a git repo with `hub-marketplace.json` at its root.

  {"name": "alice", "description": "…", "owner": {"name": "…", "url": "…"}, "hub": "^1.31",
   "plugins": [{"name": "x", "type": "plugin", "source": {"github": "alice/hub-plugin-x", "sha": "<40 hex>"}, …},
               {"name": "notes", "type": "module", "source": {"git": "https://…/m.git", "sha": "…", "subdir": "notes"}},
               {"name": "dark", "source": "./plugins/dark"}]}

An entry is pinned to a commit (`sha`): exactly that one is installed, not the author's HEAD; `./path` is a folder of the
same catalog at its commit. An external entry without `sha` is visible but marked "not pinned" (an error in strict
validation). A broken entry is skipped on its own, with a reason; unknown fields are ignored silently (a catalog of a newer
version is read by an older hub). The official catalog's names (`agents-hub`, `official`, `hub` and similar) are allowed
only from the official source.

Here: the format and the storage (slice S1): `parse` and `validate` (`hub marketplace validate <folder>`,
`python3 -m core.marketplace validate <folder> [--strict]` - the same as the catalog's CI), the catalog on disk -
`<state>/marketplaces/<name>/repo.git` (a bare clone, files come from the commit's git objects, there is no working copy),
an entry's code - the storage `<state>/plugins/` (core/codestore.py, strict folder check and limits). The connect and
install commands are the following slices.
"""
import collections.abc
import hashlib
import json
import os
import re
import sys

from core import codestore, i18n, version

FILE = "hub-marketplace.json"
DIR = "marketplaces"
PLUGINS = "plugins"
MAX_FILE = 2 * codestore.MB
# entry limits (spec section 3.3, S1 measurements): the source's bare clone, the unpacked commit and its file count
LIMITS = {"clone": 200 * codestore.MB, "unpacked": 50 * codestore.MB, "files": 5000}
LONG_PATH = 150         # a file path from the entry root: longer gives a warning (Windows, MAX_PATH)
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
RESERVED = {"agents-hub", "official", "hub"}
LOOKALIKE = re.compile(r"(agents-?)?hub-(marketplace|plugins|official)")
GITHUB = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+")
REMOTE = re.compile(r"(?:https?|ssh|git)://[^\s/]|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:")
DEVICE = re.compile(r"con|prn|aux|nul|com[0-9]|lpt[0-9]")   # Windows device names: they cannot become a folder
SEGMENT = re.compile(r"[A-Za-z0-9_.@+-]+")
ICONS = {".svg", ".png", ".webp"}
TYPES = ("plugin", "module", "skillset")
# catalogs of the agents' own formats (owner 2026-11-17: "the hub must support Claude's and Codex's marketplaces out of
# the box"): read when the repo has no hub-marketplace.json; each plugin is a skill set entry (type skillset) — a plugin
# of Claude Code or Codex given to sessions as is, with no hub-plugin.json
FOREIGN = ((os.path.join(".claude-plugin", "marketplace.json"), "claude"),
           (os.path.join(".agents", "plugins", "marketplace.json"), "codex"))
PLUGIN_MANIFESTS = (os.path.join(".claude-plugin", "plugin.json"), os.path.join(".codex-plugin", "plugin.json"))
# a plugin entry of a foreign catalog with its parts in the catalog itself: --plugin-dir does not see them
INLINE = ("hooks", "mcpServers", "lspServers", "monitors")


class Words(collections.abc.Mapping):
    """A read-only {key: text} whose texts come from the catalog (core/i18n.py) when they are read, not when the module is
    imported: a table of display words made at import would be in the language of that moment. `keys` is {key: catalog key}."""

    def __init__(self, keys):
        self.keys_of = dict(keys)

    def __getitem__(self, key):
        return i18n.t(self.keys_of[key])

    def __iter__(self):
        return iter(self.keys_of)

    def __len__(self):
        return len(self.keys_of)

    def __contains__(self, key):
        return key in self.keys_of


# a module entry's `rights` are lists of strings: what module.json asks for (core/modsrc.promised checks) and what to call it
MODULE_RIGHTS = Words({"capabilities": "core.marketplace.right_capabilities", "env": "core.marketplace.right_env"})
MANIFESTS = {"plugin": "hub-plugin.json", "module": "module.json"}
TEXT = {"version": 40, "description": 500, "category": 40, "author": 80}
# entry categories - the groups and filter of "Plugins" (the page, js/plugins.js): look and themes, feed and chat, session
# list and fleet, notifications and Telegram, tasks and work overview, agent tools, fun, other. An unknown one is a warning
# (an error for our catalog, --strict), on the page it goes to "Other"
CATEGORIES = ("look", "feed", "sessions", "notify", "work", "tools", "fun", "other")


class Invalid(RuntimeError):
    """The catalog cannot be read as a whole: not JSON, no name or entry list, a foreign use of an official name."""


def reserved(name):
    """Why a name is only for the official catalog, or None."""
    n = str(name)
    if any(ord(c) > 127 for c in n):
        return "non-ASCII character in the name (a look-alike letter is a spoofed name)"
    if n in RESERVED:
        return f"the name \"{n}\" belongs to the official catalog"
    if LOOKALIKE.match(n) or ("official" in n and ("hub" in n or "agents" in n)):
        return f"the name \"{n}\" looks like the official catalog"
    return None


def _bad_name(name):
    """Why a name does not fit as a catalog name (and its folder), or None."""
    if not NAME.fullmatch(str(name)):
        return f"name: \"{name}\" — lowercase Latin letters, digits and a hyphen, up to 41 characters"
    if DEVICE.fullmatch(str(name)):
        return f"name: \"{name}\" is a Windows device name, it cannot be a catalog folder"
    return None


def _path(raw):
    """A path inside the repo ("a/b", "" - the root) or None: no "..", absolute paths or Windows drives."""
    p = str(raw).replace("\\", "/")
    if p.startswith("/") or re.match(r"[A-Za-z]:", p):
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." or not SEGMENT.fullmatch(x) or re.match(r"[A-Za-z]:$", x) for x in parts):
        return None
    return "/".join(parts)


def _source(raw, strict):
    """(source, pinned, warnings) or RuntimeError with a reason."""
    if isinstance(raw, str):
        text = raw.replace("\\", "/")
        if not text.startswith("./"):
            raise RuntimeError("source: \"./path\" inside the catalog or {github|git, sha}")
        path = _path(text[2:])
        if path is None:
            raise RuntimeError(f"source: \"{raw}\" — a folder inside the catalog, with no \"..\", absolute paths or C:")
        return {"kind": "path", "path": path}, True, []
    if not isinstance(raw, dict):
        raise RuntimeError("source: \"./path\" inside the catalog or {github|git, sha}")
    if ("github" in raw) == ("git" in raw):
        raise RuntimeError("source: one of github or git")
    if "github" in raw:
        gh = raw["github"]
        if not (isinstance(gh, str) and GITHUB.fullmatch(gh) and gh.split("/")[1].strip(".")):
            raise RuntimeError("source.github: \"owner/repo\"")
        out = {"kind": "github", "github": gh, "url": f"https://github.com/{gh}.git"}
    else:
        url = raw["git"]
        if not isinstance(url, str) or not url.strip() or url.startswith("-") or re.search(r"[\s\x00-\x1f]", url):
            raise RuntimeError("source.git: a git repository address")
        if codestore.is_local(url):
            raise RuntimeError("source.git: a local path cannot be installed from a catalog — use https://, ssh:// or git@…; "
                               "the catalog's own code is \"./path\"")
        if not REMOTE.match(url):   # and not "<helper>::address": git would call git-remote-<helper> on the owner's machine
            raise RuntimeError("source.git: https://, http://, ssh://, git:// or git@server:path")
        out = {"kind": "git", "url": url}
    sha, ref, sub = raw.get("sha"), raw.get("ref"), raw.get("subdir")
    if sha is not None:
        if not (isinstance(sha, str) and codestore.COMMIT.fullmatch(sha.lower())):
            raise RuntimeError("source.sha: a full commit, 40 or 64 hex")
        out["sha"] = sha.lower()
    if ref is not None:
        if not (isinstance(ref, str) and codestore.REF.fullmatch(ref) and ".." not in ref):
            raise RuntimeError("source.ref: a branch or a tag")
        out["ref"] = ref
    if sub is not None:
        path = _path(sub) if isinstance(sub, str) else None
        if not path:
            raise RuntimeError("source.subdir: a folder inside the repo, with no \"..\" or absolute paths")
        out["subdir"] = path
    if "sha" in out:
        return out, True, []
    if strict:
        raise RuntimeError("source.sha: required — the entry is installed at exactly this commit")
    if re.match(r"(?:http|git)://", out["url"]):   # without encryption the code can be swapped in transit; sha would catch a swap
        raise RuntimeError("source.sha: required for http:// and git:// — without encryption the code can be swapped in transit")
    return out, False, ["not pinned (no sha): the author's current commit is installed, updates come from the author "
                        "bypassing the catalog — only through a code check"]


def _display(raw, out):
    """Fields for the screen and search: a wrong type - the field is dropped with a warning, the entry stays."""
    warn = []
    for key, cap in TEXT.items():
        if key in raw:
            if isinstance(raw[key], str) and raw[key].strip():
                out[key] = raw[key].strip()[:cap]
            else:
                warn.append(f"{key}: not a string — skipped")
    if out.get("category") and out["category"] not in CATEGORIES:
        warn.append(f"category: one of {', '.join(CATEGORIES)} — on the page the entry goes under \"Other\" (a theme goes under \"Look and themes\")")
    if "tags" in raw:
        tags = raw["tags"]
        if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
            out["tags"] = [t.strip()[:40] for t in tags if t.strip()][:20]
        else:
            warn.append("tags: not a list of strings — skipped")
    if "icon" in raw:
        icon = _path(raw["icon"]) if isinstance(raw["icon"], str) and "://" not in raw["icon"] else None
        if icon and os.path.splitext(icon)[1].lower() in ICONS:
            out["icon"] = icon
        else:
            warn.append("icon: a .svg, .png or .webp file in the catalog repo, not an outside link — skipped")
    if "hubs" in raw:
        n = raw["hubs"]
        if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
            out["hubs"] = n
        else:
            warn.append("hubs: an integer ≥ 0 — skipped")
    if "rights" in raw:
        if isinstance(raw["rights"], dict):
            out["rights"] = raw["rights"]
        else:
            warn.append("rights: not an object — skipped")
    if "revoked" in raw:
        if isinstance(raw["revoked"], str) and raw["revoked"].strip():
            out["revoked"] = raw["revoked"].strip()[:300]
        else:
            warn.append("revoked: the reason as a string — skipped")
    return warn


def parse(data, official=False, strict=False):
    """A catalog from parsed JSON -> {name, description?, owner?, hub?, entries, skipped, warnings}: entries - the
    entries that can be read ({name, type, source, pinned, ..., warnings}), skipped - [{entry, why}] of skipped ones.
    official - a catalog from the official source (it may use its name); strict - checking our catalog: an external source
    without sha is an error. Cannot be read as a whole - Invalid."""
    if not isinstance(data, dict):
        raise Invalid("the catalog is not a JSON object")
    name = data.get("name")
    if not isinstance(name, str):
        raise Invalid("name: the catalog name is a string")
    why = reserved(name)
    if why and (why.startswith("non-ASCII") or not official):
        raise Invalid(why + (" — it is read only from the official source" if not why.startswith("non-ASCII") else ""))
    if _bad_name(name):
        raise Invalid(_bad_name(name))
    if not isinstance(data.get("plugins"), list):
        raise Invalid("plugins: a list of entries")
    out = {"name": name, "entries": [], "skipped": [], "warnings": []}
    if isinstance(data.get("description"), str):
        out["description"] = data["description"].strip()[:500]
    owner = data.get("owner")
    if isinstance(owner, dict):
        out["owner"] = {k: str(owner[k])[:200] for k in ("name", "url") if isinstance(owner.get(k), str)}
    elif owner is not None:
        out["warnings"].append("owner: not an object — skipped")
    if data.get("hub") is not None:
        state, text = version.compat(data["hub"]) if isinstance(data["hub"], str) else ("unknown", "")
        if state == "incompatible":
            raise Invalid(f"the catalog is for another Plugin API version: {text}")
        if state == "newer":
            out["warnings"].append(f"the catalog is for API {data['hub']}, the hub has {version.PLUGIN_API} — update the hub")
        elif state == "ok":
            out["hub"] = data["hub"]
        else:
            out["warnings"].append(f"hub: unclear API version \"{data['hub']}\" — skipped")
    seen = set()
    for i, raw in enumerate(data["plugins"]):
        label = raw["name"] if isinstance(raw, dict) and isinstance(raw.get("name"), str) and raw["name"] else f"#{i + 1}"
        try:
            if not isinstance(raw, dict):
                raise RuntimeError("the entry is not an object")
            if not NAME.fullmatch(str(raw.get("name") or "")):
                raise RuntimeError("name: lowercase Latin letters, digits and a hyphen, up to 41 characters")
            if raw["name"] in seen:
                raise RuntimeError("name: already in the catalog above")
            kind = raw.get("type", "plugin")
            if kind not in TYPES:
                raise RuntimeError(f"type: {' or '.join(TYPES)}")
            if "source" not in raw:
                raise RuntimeError("source: missing")
            source, pinned, warn = _source(raw["source"], strict)
        except RuntimeError as e:
            out["skipped"].append({"entry": label, "why": str(e)})
            continue
        seen.add(raw["name"])
        e = {"name": raw["name"], "type": kind, "source": source, "pinned": pinned}
        warn += _display(raw, e)
        bad = [k for k, v in (e.get("rights") or {}).items() if kind == "module" and k in MODULE_RIGHTS
               and not (isinstance(v, list) and all(isinstance(x, str) for x in v))]
        if bad:   # the hub checks them against module.json (core/modsrc.promised): a promise of the wrong form promises nothing
            e["rights"] = {**e["rights"], **{k: [] for k in bad}}
            warn.append(f"rights: for a module, {', '.join(bad)} must be a list of strings; taken as promising nothing")
        e["warnings"] = warn
        out["entries"].append(e)
    return out


def loads(raw, official=False, strict=False):
    """A catalog from the file's bytes: larger than MAX_FILE or not JSON - Invalid."""
    if len(raw) > MAX_FILE:
        raise Invalid(f"{FILE} is over {MAX_FILE // codestore.MB} MB")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise Invalid(f"{FILE}: not JSON — {e}") from None
    return parse(data, official=official, strict=strict)


# --- catalog folder validation (the author, CI) --------------------------------------------------------------------

def _long(root):
    """File paths from the entry root longer than LONG_PATH."""
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in (".git", "__pycache__"))
        out += [r for r in (os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/") for n in sorted(names))
                if len(r) > LONG_PATH]
    return out


def _check_folder_entry(folder, e):
    """Errors and warnings of a "./path" entry that are visible only from the catalog's files."""
    root = os.path.join(folder, *e["source"]["path"].split("/")) if e["source"]["path"] else folder
    if not os.path.isdir(root):
        return [f"no folder ./{e['source']['path']}"], []
    man = os.path.join(root, MANIFESTS[e["type"]])
    errors, css = [], []
    if e["type"] == "plugin":
        from core import pluginstyle
        from modules.market import market
        try:
            with open(man, encoding="utf-8") as f:
                raw = json.load(f)
                if market.valid(raw) is None:
                    errors.append(f"{MANIFESTS['plugin']}: invalid plugin manifest (name, web, run, themes, skills)")
                elif raw.get("css") is not None and not raw.get("web"):
                    css = ["css: ignored — the page applies it only for a plugin with a page module (web)"]
                elif raw.get("css") is not None and raw["css"] != market.valid(raw)["css"]:   # dropped by the page, not refused
                    css = [f"css: the page takes up to {pluginstyle.CSS_MAX} distinct .css files inside the plugin folder (Latin letters, digits, _ . / -); "
                           f"the others are dropped"]
        except (OSError, ValueError) as err:
            errors.append(f"{MANIFESTS['plugin']}: {err.strerror if isinstance(err, OSError) else err}")
    elif not os.path.isfile(man) and not any(os.path.isfile(os.path.join(root, d, MANIFESTS["module"]))
                                             for d in (os.listdir(root) if os.path.isdir(root) else [])):
        errors.append(f"no {MANIFESTS['module']} (neither in the folder nor in its subfolders)")
    long = _long(root)
    warn = css + ([f"paths longer than {LONG_PATH} characters from the entry root (Windows): {', '.join(long[:3])}"
                   + (f" and others: {len(long) - 3}" if len(long) > 3 else "")] if long else [])
    return errors, warn


def validate(folder, strict=False):
    """Checking a catalog folder before publishing -> (lines, ok): errors (skipped entries, the catalog cannot be read) -
    ok False; warnings - only with strict."""
    path = os.path.join(folder, FILE)
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE + 1)
    except OSError as e:
        return [f"{path}: {e.strerror or e}"], False
    try:
        cat = loads(raw, official=True, strict=strict)
    except Invalid as e:
        return [f"{FILE}: {e}"], False
    why = reserved(cat["name"])
    lines = [f"· {why}: the hub reads this catalog only from the official source"] if why else []
    errors = [f"✗ {s['entry']}: {s['why']}" for s in cat["skipped"]]
    warns = [f"! {w}" for w in cat["warnings"]]
    good = 0
    for e in cat["entries"]:
        bad = []
        if e["source"]["kind"] == "path":
            bad, more = _check_folder_entry(folder, e)
            e["warnings"] += more
        icon = e.get("icon")
        if icon and not os.path.isfile(os.path.join(folder, *icon.split("/"))):
            e["warnings"].append(f"icon: no file {icon} in the catalog")
        errors += [f"✗ {e['name']}: {b}" for b in bad]
        warns += [f"! {e['name']}: {w}" for w in e["warnings"]]
        good += not bad
    ok = not errors and not (strict and warns)
    head = f"{cat['name']}: {len(cat['entries']) + len(cat['skipped'])} entries, fine: {good}"
    tail = [] if ok else ["failed" + (" (--strict: warnings are errors too)" if strict and not errors else "")]
    return [head] + lines + errors + warns + tail, ok


# --- the catalog on disk ----------------------------------------------------------------------------------------------

def store_root(state_dir):
    return os.path.join(state_dir, DIR)


def catalog_dir(state_dir, name):
    return os.path.join(store_root(state_dir), name)


def _repo(state_dir, name):
    return os.path.join(catalog_dir(state_dir, name), codestore.REPO_DIR)


def _busy(state_dir):
    """One command over the catalogs at a time - the root lock, as in the code storage."""
    return codestore.Store(state_dir, DIR).busy()


def _blob(repo, commit, path):
    """A file of the commit -> bytes, or None if there is none; larger than MAX_FILE — Invalid."""
    path = path.replace(os.sep, "/")
    r = codestore.git(f"--git-dir={repo}", "cat-file", "-s", f"{commit}:{path}")
    if r.returncode:
        return None
    if int(r.stdout.strip() or 0) > MAX_FILE:
        raise Invalid(i18n.t("core.marketplace.file_big", file=path, mb=MAX_FILE // codestore.MB))
    r = codestore.git(f"--git-dir={repo}", "cat-file", "blob", f"{commit}:{path}", binary=True)
    if r.returncode:
        raise Invalid(f"{path}: {codestore.said(r)}")
    return r.stdout


def _json(raw, path):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise Invalid(f"{path}: not JSON — {e}") from None


def _foreign_source(raw):
    """A plugin source of a Claude Code or Codex catalog -> the hub's form (_source reads it), or RuntimeError."""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        raise RuntimeError("source: a path or an object")
    kind, pin = raw.get("source"), {k: raw[k] for k in ("ref", "sha") if raw.get(k) is not None}
    if kind == "local":
        return raw.get("path")
    if kind == "github":
        return {"github": raw.get("repo"), **pin}
    if kind in ("url", "git"):
        return {"git": raw.get("url"), **pin}
    if kind == "git-subdir":
        url = raw.get("url")
        where = {"github": url} if isinstance(url, str) and GITHUB.fullmatch(url) else {"git": url}
        return {**where, "subdir": raw.get("path"), **pin}
    if kind == "command":
        raise RuntimeError("source command: installed by running a command on the machine — the hub does not do that")
    raise RuntimeError(f"source {kind!r}: the hub installs a path, github, url and git-subdir")


def _text(x):
    """A display field of a foreign entry: a string, or the name of an {name, …} object (author)."""
    return x if isinstance(x, str) else x.get("name") if isinstance(x, dict) and isinstance(x.get("name"), str) else None


def foreign(data, kind):
    """A catalog of Claude Code (.claude-plugin/marketplace.json) or Codex (.agents/plugins/marketplace.json) -> (the
    hub's form for parse, skipped [{entry, why}]): every plugin a skill set entry; what the hub does not install is
    skipped with the reason."""
    if not isinstance(data, dict):
        raise Invalid("the catalog is not a JSON object")
    out, skipped = {k: data[k] for k in ("name", "owner") if k in data}, []
    if isinstance(data.get("description"), str):
        out["description"] = data["description"]
    elif isinstance((data.get("metadata") or {}).get("description"), str):
        out["description"] = data["metadata"]["description"]
    plugins = data.get("plugins")
    out["plugins"] = []
    for i, p in enumerate(plugins if isinstance(plugins, list) else []):
        label = p.get("name") if isinstance(p, dict) and isinstance(p.get("name"), str) else f"#{i + 1}"
        try:
            if not isinstance(p, dict):
                raise RuntimeError("the entry is not an object")
            if p.get("strict") is False or any(k in p for k in INLINE):
                raise RuntimeError("the plugin's parts are described in the catalog, not in the plugin — not supported")
            if ((p.get("policy") or {}) if isinstance(p.get("policy"), dict) else {}).get("installation") == "NOT_AVAILABLE":
                raise RuntimeError("the catalog marks it not available")
            e = {"name": p.get("name"), "type": "skillset", "source": _foreign_source(p.get("source"))}
        except RuntimeError as err:
            skipped.append({"entry": label, "why": str(err)})
            continue
        for k in ("description", "version", "author"):
            if _text(p.get(k)):
                e[k] = _text(p[k])
        tags = [t for t in (p.get("keywords") or p.get("tags") or []) if isinstance(t, str)] \
            if isinstance(p.get("keywords") or p.get("tags"), list) else []
        if tags:
            e["tags"] = tags
        out["plugins"].append(e)
    if not isinstance(plugins, list):
        out["plugins"] = None   # parse names it: "plugins: a list of entries"
    return out, skipped


def _single(name, man):
    """A repo that is one plugin -> a one-entry catalog in the hub's form."""
    e = {"name": name, "type": "skillset", "source": "./"}
    for k in ("description", "version", "author"):
        if _text((man or {}).get(k)):
            e[k] = _text(man[k])
    return {"name": name, "description": e.get("description") or "", "plugins": [e]}


def _repo_name(url):
    """A catalog name from a repo address: its last part, lowercase, letters, digits and hyphens."""
    base = re.sub(r"\.git$", "", str(url or "").rstrip("/\\").replace("\\", "/").split("/")[-1].split(":")[-1])
    return re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-")[:41] or "skills"


def _read(repo, commit, official):
    """The catalog at the commit: hub-marketplace.json, else a catalog of Claude Code or Codex, else a repo that is one
    plugin (a manifest at the root, or only skills/<name>/SKILL.md folders) — a catalog of one entry."""
    raw = _blob(repo, commit, FILE)
    if raw is not None:
        return loads(raw, official=official)
    for path, kind in FOREIGN:
        raw = _blob(repo, commit, path)
        if raw is not None:
            data, skipped = foreign(_json(raw, path), kind)
            cat = parse(data, official=official)
            cat["skipped"] = skipped + cat["skipped"]
            cat["format"] = kind
            return cat
    for path in PLUGIN_MANIFESTS:
        raw = _blob(repo, commit, path)
        if raw is not None:
            man = _json(raw, path)
            name = man.get("name") if isinstance(man, dict) else None
            good = isinstance(name, str) and NAME.fullmatch(name) and not reserved(name)
            cat = parse(_single(name if good else _repo_name(_origin(repo)), man), official=official)
            cat["format"] = "plugin"
            return cat
    r = codestore.git(f"--git-dir={repo}", "ls-tree", "-r", "--name-only", commit, "--", "skills")
    if not r.returncode and any(re.fullmatch(r"skills/[^/]+/SKILL\.md", x) for x in r.stdout.splitlines()):
        cat = parse(_single(_repo_name(_origin(repo)), None), official=official)
        cat["format"] = "skills"
        return cat
    raise Invalid(i18n.t("core.marketplace.no_file", commit=commit[:12], file=" / ".join(
        [FILE, *(p.replace(os.sep, "/") for p, _ in FOREIGN), "skills/<name>/SKILL.md"])))


def _origin(repo):
    r = codestore.git(f"--git-dir={repo}", "config", "--get", "remote.origin.url")
    return r.stdout.strip() if not r.returncode else None


def fetch(state_dir, url, ref=None, name=None, official=False):
    """A fresh catalog commit into its bare clone -> {name, commit, catalog}. name - the catalog is already connected (an
    update): the clone is its own, the file must name the same name. Without name - a new one: the clone goes into a
    temporary folder, the name comes from the file; taken by another source - a refusal. The catalog cannot be read -
    Invalid, the previous commit in the clone stays readable."""
    with _busy(state_dir):
        if name is not None:
            if _bad_name(name):
                raise RuntimeError(_bad_name(name))
            commit, _ = codestore.fetch_capped(_repo(state_dir, name), url, ref, LIMITS["clone"])
            cat = _read(_repo(state_dir, name), commit, official)
            if cat["name"] != name:
                raise Invalid(i18n.t("core.marketplace.name_mismatch", said=cat["name"], name=name))
            return {"name": name, "commit": commit, "catalog": cat}
        tmp = os.path.join(store_root(state_dir), f".new-{hashlib.sha1(str(url).encode()).hexdigest()[:8]}-{os.getpid()}")
        codestore.remove(tmp)
        try:
            commit, _ = codestore.fetch_capped(os.path.join(tmp, codestore.REPO_DIR), url, ref, LIMITS["clone"])
            cat = _read(os.path.join(tmp, codestore.REPO_DIR), commit, official)
            target = catalog_dir(state_dir, cat["name"])
            if os.path.exists(target):
                if _origin(_repo(state_dir, cat["name"])) != url:
                    raise RuntimeError(i18n.t("core.marketplaces.name_taken", name=cat["name"], source=codestore.shown(
                        _origin(_repo(state_dir, cat["name"])) or "?")))
                codestore.remove(target)
            os.replace(tmp, target)
        finally:
            codestore.remove(tmp)
        return {"name": cat["name"], "commit": commit, "catalog": cat}


def read(state_dir, name, commit, official=False):
    """The catalog at the pinned commit - from the clone's git objects (there is no working copy)."""
    return _read(_repo(state_dir, name), commit, official)


# --- an entry's code-------------------------------------------------------------------------------------------------------

def plugins_store(state_dir):
    """The storage of entries' code: <state>/plugins/ - a separate root (the cleanup of the earlier core knows only
    modules/), an extra file in a folder counts as an edit, the LIMITS limits apply."""
    return codestore.Store(state_dir, PLUGINS, strict=True, limits=LIMITS,
                           fix=i18n.t("core.marketplace.fix_reinstall"))


def source_of(catalog_url, catalog_commit, entry):
    """Where an entry's code is installed from -> {url, sha?, ref?, subdir?}: "./path" is the catalog itself at its commit."""
    s = entry["source"]
    if s["kind"] == "path":
        return {"url": catalog_url, "sha": catalog_commit, **({"subdir": s["path"]} if s["path"] else {})}
    return {k: s[k] for k in ("url", "sha", "ref", "subdir") if k in s}


def _have(repo, sha):
    if not os.path.isfile(os.path.join(repo, "HEAD")):
        return False
    r = codestore.git(f"--git-dir={repo}", "rev-parse", "-q", "--verify", f"{sha}^{{commit}}")
    return not r.returncode and r.stdout.strip() == sha


def pin(store, url, sha):
    """Exactly the source's commit sha in the storage's bare clone -> sha; the author does not have it - a refusal. Called under store.busy()."""
    if _have(store.git_dir(url), sha):
        return sha   # the commit is already in the clone: sha is a content hash, no network needed (a reinstall)
    try:
        commit, _ = store.fetch(url, sha)
    except RuntimeError as e:
        if "has no branch, tag or commit" not in str(e):   # the words of core/codestore.fetch_into - change both together
            raise
        raise RuntimeError(i18n.t("core.marketplace.pin_gone", sha=sha[:12], source=codestore.shown(url))) from None
    if commit != sha:
        raise RuntimeError(i18n.t("core.marketplace.pin_not_commit", source=codestore.shown(url), sha=sha[:12],
                                  commit=commit[:12]))
    return sha


def fetch_code(store, src, name=None):
    """An entry's code into the storage -> (commit, entry folder). With sha - exactly this commit (the author does not have
    it - a refusal), without - the current commit of the author's ref. name - a skill set entry: only its folder of the
    commit, as <...>/<name> (codestore.Store.commit_dir part): a plugin of a big catalog is not the whole catalog."""
    url, sha, sub = src["url"], src.get("sha"), src.get("subdir")
    with store.busy():
        commit = pin(store, url, sha) if sha else store.fetch(url, src.get("ref"))[0]
        if name:
            try:
                folder = os.path.join(store.unpack(url, commit, (sub, name)), name)
            except RuntimeError as e:
                if "Not a valid object name" not in str(e) and "not a tree object" not in str(e):
                    raise
                raise RuntimeError(i18n.t("core.marketplace.no_folder", source=codestore.shown(url), commit=commit[:12],
                                          sub=sub)) from None
            return commit, folder
        folder = store.unpack(url, commit)
    root = os.path.join(folder, *sub.split("/")) if sub else folder
    if not os.path.isdir(root):
        raise RuntimeError(i18n.t("core.marketplace.no_folder", source=codestore.shown(url), commit=commit[:12], sub=sub))
    return commit, root


# --- CLI for the catalog's CI -------------------------------------------------------------------------------------------

def main(argv):
    """python3 -m core.marketplace validate <folder> [--strict] -> exit code."""
    args = [a for a in argv if a != "--strict"]
    if len(args) != 2 or args[0] != "validate":
        print("hub marketplace validate <folder> [--strict]   check a catalog (hub-marketplace.json) before publishing",
              file=sys.stderr)
        return 2
    lines, ok = validate(args[1], strict="--strict" in argv)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
