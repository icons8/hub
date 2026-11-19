"""Settings -> Plugins, "Claude and Codex plugins": what the hub installed from the engines' own catalogs (the built-in
claude-plugins-official and openai-curated, core/marketplaces.BUILTIN_SOURCES) and what is inside each plugin.

An install of such an entry is a `sets` record with `plugin: true`, `market` the catalog and `entry` the plugin
(core/mpinstall); its tool id is <entry>@<the set's name> (core/catalog._one_plugin), the one `hub tools grant` takes.
What is inside is read from the plugin's own folder: skills and commands in Claude Code's default folders and the paths
the manifest adds, MCP servers as a session gets them (core/catalog.declared_servers), and what will not work as
shipped — a placeholder left in a server's settings instead of the data its sign-in needs (`<SOME_CLIENT_ID>`). Facts
only: the page words them in its language.

The page server calls it: no hub imports at the top (a new name in an old module would break the old process)."""
import json
import os
import re

PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]{2,}>")   # <ZOOM_PUBLIC_CLIENT_ID>: an author's blank, not a value
MANIFESTS = (os.path.join(".claude-plugin", "plugin.json"), os.path.join(".codex-plugin", "plugin.json"))
MAX_JSON = 1 << 20
SHOWN = 40   # names of one kind the page gets; the count is whole


def catalogs(cfg):
    """Names of the engines' catalogs connected in hub.json: a record marked builtin or one of the built-in names (the
    names hold when HUB_NO_OFFICIAL_MARKETPLACE turned the built-in ones off for new hubs)."""
    from core.marketplaces import BUILTIN_SOURCES
    mps = cfg.get("marketplaces") if isinstance(cfg.get("marketplaces"), dict) else {}
    return set(BUILTIN_SOURCES) | {n for n, m in mps.items() if isinstance(m, dict) and m.get("builtin") is True}


def installed(cfg):
    """[{key, entry, catalog, description, version, missing, skills, commands, mcp, counts, warnings}] — the one-plugin
    sets installed from the engines' catalogs, by key. hub.json edited by hand: a record of the wrong shape is skipped."""
    from core.marketplaces import set_key   # <entry>@<the set's name>: the core's tool id (catalog._one_plugin)
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    mine = catalogs(cfg)
    out = []
    for s in sets:
        if not (isinstance(s, dict) and s.get("plugin") is True and s.get("market") in mine
                and isinstance(s.get("name"), str) and isinstance(s.get("entry"), str) and s["entry"]
                and isinstance(s.get("path"), str)):
            continue
        path = os.path.expanduser(s["path"])
        empty = {"skills": [], "commands": [], "mcp": [], "warnings": [], "description": ""}
        try:
            got = contents(path) if os.path.isdir(path) else empty
        except (OSError, RecursionError):   # a folder it cannot read, JSON nested too deep: the row stays, empty
            got = empty
        commit = s.get("commit") if isinstance(s.get("commit"), str) else ""
        out.append({"key": set_key(s), "entry": s["entry"], "catalog": s["market"],
                    "description": got.pop("description"),
                    "version": s["version"] if isinstance(s.get("version"), str) else commit[:7],
                    "missing": not os.path.isdir(path),
                    "counts": {k: len(got[k]) for k in ("skills", "commands", "mcp")},
                    **{k: got[k][:SHOWN] for k in ("skills", "commands", "mcp")}, "warnings": got["warnings"]})
    return sorted(out, key=lambda x: x["key"])


def _inside(root, rel):
    """rel inside the plugin folder (after links) -> its path, or None: a manifest does not lead the page outside."""
    if not isinstance(rel, str) or not rel:
        return None
    real, base = os.path.realpath(os.path.join(root, rel)), os.path.realpath(root)
    try:
        return real if os.path.commonpath([real, base]) == base else None
    except ValueError:   # Windows: another drive
        return None


def _json(path):
    try:
        if not path or os.path.getsize(path) > MAX_JSON:
            return None
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _paths(root, value, default):
    """A manifest key (a path or a list of them) -> folders/files inside the plugin: Claude Code's default folder and
    what the key adds to it (custom paths supplement the default one, they do not replace it)."""
    rels = [default, *(value if isinstance(value, list) else [value] if isinstance(value, str) else [])]
    return list(dict.fromkeys(p for p in (_inside(root, r) for r in rels if isinstance(r, str)) if p))


def contents(root):
    """What one plugin folder holds -> {description, skills, commands, mcp, warnings}: sorted names, a skill by its
    folder (with SKILL.md), a command by its .md file, a server by its name in mcpServers; a warning
    {code: "placeholder", server, value} per server whose settings still carry an author's blank."""
    from core import catalog
    # Claude Code's manifest, else Codex's (catalog._manifest_of): a plugin is read by one of them, not by both
    man = next((m for m in (_json(_inside(root, rel)) for rel in MANIFESTS) if isinstance(m, dict)), {})
    skills = set()
    for d in _paths(root, man.get("skills"), "skills"):
        if os.path.isfile(os.path.join(d, "SKILL.md")):
            skills.add(os.path.basename(os.path.normpath(d)))
        elif os.path.isdir(d):
            skills |= {n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n, "SKILL.md"))}
    commands = set()
    for d in _paths(root, man.get("commands"), "commands"):
        files = [d] if os.path.isfile(d) else [os.path.join(d, n) for n in os.listdir(d)] if os.path.isdir(d) else []
        commands |= {os.path.basename(f)[:-3] for f in files if f.endswith(".md") and os.path.isfile(f)
                     and not os.path.basename(f).startswith(("_", "."))}   # _conventions.md: notes for the others
    servers = catalog.declared_servers(root)   # the same list a session gets (catalog.plugin_servers), as written
    warnings = []
    for name, conf in sorted(servers.items()):
        for value in sorted(set(PLACEHOLDER.findall(json.dumps(conf, ensure_ascii=False)))):
            warnings.append({"code": "placeholder", "server": str(name), "value": value})
    about = man.get("description")
    face = man.get("interface") if isinstance(man.get("interface"), dict) else {}
    about = about if isinstance(about, str) and about else face.get("shortDescription")
    return {"description": about if isinstance(about, str) else "", "skills": sorted(skills),
            "commands": sorted(commands), "mcp": sorted(str(n) for n in servers), "warnings": warnings}
