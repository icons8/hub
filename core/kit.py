"""A session's set (spec d2547143): which skills and MCP servers its process loaded, as the engine itself said it.

The host gives `session.kit` only when the set changed (the first init of a process, then a change); the fold keeps it in
`sess["kit"]`, and the page shows it next to the calls from the journal (core/uses.py, /api/session/kit).

Claude Code: the init of a turn names every skill (`skills`), the plugins with their folders, the plugins that did not
load (`plugin_errors`) and every tool (`mcp__<server>__<tool>`); its control request `get_context_usage` lists the
skills that went into the model's list (`skillFrontmatter`: source, plugin, path to SKILL.md). The two differ both ways
(17.11, Claude Code 2.1.309: a `disable-model-invocation` skill is in init only, `init` and `security-review` in the
list only), so the set is their union; `in_prompt` is said only when the list is known. Codex: `skills/list`
(name, path, scope, pluginId) and the tool count of each server in `mcpServerStatus/list`.

stdlib only, no hub imports at the top beyond what the hosts already load."""
import os
import re

from core import backbone, redact

ERROR_MAX = 300
# a skill of the parent repo, seen from an executor's working copy, comes a second time under this prefix
WORKTREE = re.compile(r"^\.claude/worktrees/[^:]+:")
# get_context_usage's sources -> the set's words
SOURCES = {"projectSettings": "repo", "plugin": "plugin", "built-in": "built-in", "bundled": "built-in",
           "userSettings": "user", "policySettings": "managed", "syncedSkills": "synced"}
CODEX_SCOPES = {"repo": "repo", "system": "built-in", "user": "user", "admin": "managed"}


def name_of(raw):
    return WORKTREE.sub("", str(raw))


def _server_key(name):
    """A server's name as Claude Code puts it into tool names (core/mcpwatch._tool_name; not imported: the hosts load
    this module, and mcpwatch brings the state machines)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(name))


def from_init(msg):
    """The init part of the set: skill names, plugin folders, plugin errors, MCP servers with their tool counts."""
    skills = list(dict.fromkeys(name_of(n) for n in msg.get("skills") or [] if isinstance(n, str)))
    plugins = {p["name"]: p.get("path") for p in msg.get("plugins") or []
               if isinstance(p, dict) and isinstance(p.get("name"), str)}
    errors = [{"plugin": str(e.get("plugin") or "")[:100], "type": str(e.get("type") or "")[:60],
               "text": redact.mask(str(e.get("message") or ""))[:ERROR_MAX]}
              for e in msg.get("plugin_errors") or [] if isinstance(e, dict)]
    servers = [s.get("name") for s in msg.get("mcp_servers") or [] if isinstance(s, dict) and isinstance(s.get("name"), str)]
    counts = {}
    for tool in msg.get("tools") or []:
        parts = str(tool).split("__")
        if len(parts) >= 3 and parts[0] == "mcp":
            counts[parts[1]] = counts.get(parts[1], 0) + 1
    return {"skills": skills, "plugins": plugins, "plugin_errors": errors,
            "mcp": [{"name": s, "tools": counts.get(_server_key(s), 0)} for s in servers]}


def from_usage(response):
    """get_context_usage's answer -> {"skills": {name: {source, plugin, path, tokens}}, "listed": {in_prompt, total,
    tokens}}; an answer without skills (none listed) is still an answer."""
    got = (response or {}).get("skills") or {}
    skills = {}
    for row in got.get("skillFrontmatter") or []:
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            one = {k: v for k, v in (
                ("source", SOURCES.get(row.get("source"), str(row.get("source") or "") or None)),
                ("plugin", row.get("pluginName")), ("path", row.get("path"))) if isinstance(v, str) and v}
            if _tokens(row.get("tokens")) is not None:   # what this entry takes of the list (a count of the host's own)
                one["tokens"] = row["tokens"]
            skills[name_of(row["name"])] = one
    listed = {"in_prompt": got.get("includedSkills"), "total": got.get("totalSkills")}
    if not all(isinstance(v, int) for v in listed.values()):
        return {"skills": skills, "listed": None}
    # what the list takes in the model's context (get_context_usage `skills.tokens`, 2.1.309: the sum of the entries'); an
    # older Claude Code does not say — None, not 0
    return {"skills": skills, "listed": {**listed, "tokens": _tokens(got.get("tokens"))}}


def _tokens(n):
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None


def build(init, usage, cwd):
    """The set the host gives: init ∪ the model's list; where a skill comes from — the list's word, else a plugin by the
    name's prefix, a repo skill on disk, else built in. usage None — the list is not known (not asked yet, an older
    Claude Code): no in_prompt."""
    init = init or {"skills": [], "plugins": {}, "plugin_errors": [], "mcp": []}
    listed = (usage or {}).get("skills") or {}
    repo = None
    rows = []
    for name in dict.fromkeys([*init["skills"], *listed]):
        row = {"name": name, **listed.get(name, {})}
        if "source" not in row:
            plugin, sep, tail = name.partition(":")
            if sep:
                row.update(source="plugin", plugin=plugin)
                path = os.path.join(init["plugins"].get(plugin) or "", "skills", tail, "SKILL.md")
                if init["plugins"].get(plugin) and os.path.isfile(path):
                    row["path"] = path
            else:
                repo = backbone.repo_skills(cwd) if repo is None else repo
                if name in repo:
                    row.update(source="repo", path=repo[name])
                else:
                    row["source"] = "built-in"
        if usage is not None:
            row["in_prompt"] = name in listed
        rows.append(row)
    rows.sort(key=lambda r: r["name"])
    out = {"skills": rows, "plugin_errors": init["plugin_errors"], "mcp": init["mcp"]}
    if usage is not None and usage.get("listed"):
        out["listed"] = usage["listed"]
    return out


def from_codex(skills, servers):
    """Codex: skills/list rows (enabled ones) and {server: tool count} from mcpServerStatus/list."""
    rows = []
    for s in skills or []:
        if not isinstance(s, dict) or not isinstance(s.get("name"), str):
            continue
        plugin = str(s.get("pluginId") or "").partition("@")[0]
        row = {"name": s["name"], "source": "plugin" if plugin else CODEX_SCOPES.get(s.get("scope"), "built-in")}
        if plugin:
            row["plugin"] = plugin
        if isinstance(s.get("path"), str):
            row["path"] = s["path"]
        rows.append(row)
    rows.sort(key=lambda r: r["name"])
    return {"skills": rows, "plugin_errors": [],
            "mcp": [{"name": n, "tools": c} for n, c in sorted((servers or {}).items())]}
