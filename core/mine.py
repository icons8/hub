"""The owner's own "superpowers" — into the hub catalog. Hub sessions do not load his Claude Code settings
(setting_sources without user): no hooks, plugins or MCP — by design. The hub sees what the owner has and, on request,
adds it to the catalog; sessions get it as needed (`hub tools grant`), with the risk and the owner's "yes" (owner decision 2026-10-02).

What is visible: plugin sets (~/.claude/plugins/known_marketplaces.json, which plugins are enabled — settings.json),
personal skills (~/.claude/skills/*), user-level MCP (~/.claude.json). MCP config values (keys in args and
headers) are not printed and are carried into hub.json as is.
"""
import json
import os
import re

import platforms
from core import catalog, i18n   # the refusals: `hub tools import` prints them in its process's language

SESSION_RISK = ["owner-data"]   # an MCP a session imports without --risk: the owner's server, his sign-in or keys
MY_SKILLS = "my-skills"  # wrapper set for personal skills: one plugin per skill, the skill itself a link, not a copy


def _home(env):
    return (env or os.environ).get("HOME") or os.path.expanduser("~")


def _claude(env):
    return (env or os.environ).get("CLAUDE_CONFIG_DIR") or os.path.join(_home(env), ".claude")


def _read(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _cfg_path(state_dir, env):
    return (env or {}).get("HUB_CONFIG") or catalog.config_path(state_dir)


def _write_cfg(state_dir, env, change, private=False):
    """Edit hub.json under the settings lock (catalog.edit_settings): reading does not give {} on error — writing over it
    would erase the projects and the owner's belt. private — the file is for the owner only (0600): it holds the imported MCP's keys."""
    with catalog.edit_settings(_cfg_path(state_dir, env), private=private) as cfg:
        change(cfg)


def _skill_description(path):
    try:
        with open(os.path.join(path, "SKILL.md"), encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return ""
    m = re.search(r"^description:[ \t]*(.*)$", head, re.M)
    if not m:
        return ""
    first = m.group(1).strip()
    if first and first[0] not in "|>":
        return first.strip("\"'")
    lines = []  # YAML block scalar (| or >): the following indented lines
    for line in head[m.end():].splitlines()[1:]:
        if line and not line[0].isspace():
            break
        lines.append(line.strip())
    return " ".join(x for x in lines if x)


def discover(state_dir, env=None):
    """{sets, skills, mcp}: what the owner has in Claude Code and whether it is already in the hub catalog."""
    cfg = _read(_cfg_path(state_dir, env), {})
    claude = _claude(env)
    hub_sets = {s.get("name") for s in cfg.get("sets") or []} | {os.path.realpath(s.get("path", ""))
                                                                  for s in cfg.get("sets") or []}
    enabled = [k for k, v in (_read(os.path.join(claude, "settings.json"), {}).get("enabledPlugins") or {}).items()
               if v]
    sets = []
    for name, m in sorted(_read(os.path.join(claude, "plugins", "known_marketplaces.json"), {}).items()):
        where = m.get("installLocation") or ""
        sets.append({"name": name, "path": where, "enabled": sorted(p.split("@")[0] for p in enabled
                                                                     if p.endswith("@" + name)),
                     "in_hub": name in hub_sets or os.path.realpath(where) in hub_sets})
    in_catalog = set()  # skills already in catalog plugins (the hub's own set, my-skills) — under any plugin
    for t in catalog.build({**catalog.DEFAULTS, **cfg}).values():
        d = os.path.join(t.get("path") or "", "skills")
        in_catalog.update(os.listdir(d) if t.get("kind") == "plugin" and os.path.isdir(d) else [])
    skills, root = [], os.path.join(claude, "skills")
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if os.path.exists(os.path.join(root, name, "SKILL.md")):
            skills.append({"name": name, "description": _skill_description(os.path.join(root, name)),
                           "in_hub": name in in_catalog})
    hub_mcp = cfg.get("mcp") or {}
    mcp = [{"name": n, "type": s.get("type") or ("stdio" if s.get("command") else "?"), "in_hub": n in hub_mcp}
           for n, s in sorted((_read(os.path.join(_home(env), ".claude.json"), {}).get("mcpServers") or {}).items())]
    # projects — folders where the owner worked with Claude Code (~/.claude.json): the hub adds them to start sessions there
    hub_paths = {os.path.realpath(p.get("path", "")) for p in (cfg.get("projects") or {}).values()}
    projects = [{"name": _project_name(p), "path": p, "in_hub": os.path.realpath(p) in hub_paths}
                for p in sorted((_read(os.path.join(_home(env), ".claude.json"), {}).get("projects") or {}))
                if os.path.exists(os.path.join(p, ".git")) and _a_project(p, state_dir)]
    return {"sets": sets, "skills": skills, "mcp": mcp, "projects": projects}


def _slashed(path):
    """A path with "/" between folders, whatever separator it was written with (Windows stores C:\\...)."""
    return str(path).replace("\\", "/").rstrip("/")


def _project_name(path):
    return _slashed(path).rsplit("/", 1)[-1]


def _a_project(path, state_dir):
    """A repository where the owner worked — not an executor's working copy and not the hub folder (by folder boundary:
    state-old is not state)."""
    p, state = (_slashed(os.path.normcase(x)) for x in (path, state_dir))
    return "/.claude/worktrees/" not in p + "/" and p != state and not p.startswith(state + "/")


def import_set(state_dir, name, env=None):
    found = next((s for s in discover(state_dir, env)["sets"] if s["name"] == name), None)
    if not found:
        raise ValueError(i18n.t("core.mine.no_set", name=name))
    if not found["in_hub"]:
        _write_cfg(state_dir, env, lambda c: c.setdefault("sets", []).append({"name": name, "path": found["path"]}))


def import_mcp(state_dir, name, about, risk, env=None, session=False):
    """A user-level MCP of the owner into the catalog's `mcp`. session — a hub session imports it, not the owner in a
    terminal: the entry already in hub.json keeps its risk and its other fields (only, exclusive, as, with) — a session
    only adds a risk — and with no risk at all it gets SESSION_RISK (18.11: `import mcp <name>` from the hub wrote a
    server with the owner's data over as riskless, and any session could then grant it to itself). The owner's import
    writes the entry as he gives it."""
    server = (_read(os.path.join(_home(env), ".claude.json"), {}).get("mcpServers") or {}).get(name)
    if not server:
        raise ValueError(i18n.t("core.mine.no_mcp", name=name))

    def put(c):
        mcp = c.setdefault("mcp", {})
        old = (mcp.get(name) if isinstance(mcp.get(name), dict) else {}) if session else {}
        mcp[name] = {**old, "server": server, "description": about,
                     "risk": _session_risk(risk, old.get("risk")) if session else sorted(risk)}
    # the server's env and headers come from ~/.claude.json (0600) — so hub.json with them is for the owner only
    _write_cfg(state_dir, env, put, private=True)


def _session_risk(given, had):
    """What a session's import writes: the given risk and the one the entry had (a list or one word), at least
    SESSION_RISK."""
    had = [had] if isinstance(had, str) else had if isinstance(had, list) else []
    out = {r for r in [*(given or []), *had] if isinstance(r, str) and r}
    return sorted(out or SESSION_RISK)


def import_skill(state_dir, name, risk, env=None, session=False):
    """A personal skill — as a plugin of the my-skills set (state_dir/my-skills): one plugin per skill, the skill a link.
    session — from a hub session: a risk the skill already had stays (what its scripts run is risky by itself,
    catalog._runs)."""
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name:
        # a folder in ~/.claude/skills, not a path: the name goes here into a path and into the set list
        raise ValueError(i18n.t("core.mine.no_skill", name=str(name)[:80]))
    src = os.path.join(_claude(env), "skills", name)
    if not os.path.exists(os.path.join(src, "SKILL.md")):
        raise ValueError(i18n.t("core.mine.no_skill", name=name))
    root = os.path.join(state_dir, MY_SKILLS)
    plugin = os.path.join(root, "plugins", name)
    os.makedirs(os.path.join(plugin, ".claude-plugin"), exist_ok=True)
    os.makedirs(os.path.join(plugin, "skills"), exist_ok=True)
    link = os.path.join(plugin, "skills", name)
    if not os.path.lexists(link):   # islink does not see a junction (Windows without the symlink right)
        platforms.current.link_dir(src, link)
    about = _skill_description(src)
    with open(os.path.join(plugin, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "description": about}, f, ensure_ascii=False)
    os.makedirs(os.path.join(root, ".claude-plugin"), exist_ok=True)
    market_path = os.path.join(root, ".claude-plugin", "marketplace.json")
    # not _read: on an unreadable file it would give an empty list, and writing over it would erase the earlier skills (review)
    market = catalog.read_settings(market_path) or {"name": MY_SKILLS, "owner": {"name": "owner"}, "plugins": []}
    if session:
        had = next((p.get("keywords") for p in market["plugins"] if p.get("name") == name), None)
        risk = {r for r in (*risk, *catalog._risk([k for k in had if isinstance(k, str)] if isinstance(had, list) else []))
                if r}
    market["plugins"] = [p for p in market["plugins"] if p["name"] != name] + [
        {"name": name, "source": f"./plugins/{name}", "description": about,
         "keywords": [f"risk:{r}" for r in sorted(risk)]}]
    with open(market_path, "w", encoding="utf-8") as f:
        json.dump(market, f, ensure_ascii=False, indent=2)

    def add(c):
        if not any(s.get("name") == MY_SKILLS for s in c.get("sets") or []):
            c.setdefault("sets", []).append({"name": MY_SKILLS, "path": root})
    _write_cfg(state_dir, env, add)
