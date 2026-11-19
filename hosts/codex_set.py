"""Codex: what a Codex session of the hub gets besides its prompt — the
hub's Codex folder config.toml, the set from the catalog (plugins' skills, MCP servers) — for the codex host
(hosts/codex.py).

Seams the host calls:
- write_config(home) — config.toml of the hub's Codex folder (CONFIG), on every CodexHost start;
- collect(flags, skill_roots, mcp, env, cwd, trust_at) — the session's set from claude's flags (the catalog) and its
  folder, as a Claude session of the hub gets it: plugins -> Codex skill roots, --mcp-config -> mcp_servers, the repo's
  .claude/skills -> skill roots; returns `extra` for thread_config: plugin and repo hooks, skills switched off; env —
  the session's environment (gets CLAUDE_PROJECT_DIR when there are hooks); trust_at — the hooks' trust key path;
- args(enabled) — extra arguments of the session's `codex app-server` command line; enabled — the features the session
  turns on (`--enable`);
- thread_config(mcp, env, **extra) — the `config` of thread/start | thread/resume (empty — no `config`).

Plugins and the repo (slice J, probes 2026-11-05 on codex 0.162.1 with a fake Responses API):
- skills: Codex reads none of .claude/skills, and from the catalog plugins it took only skills/; the roots go by
  skills/extraRoots/set. Codex ignores Claude's `disable-model-invocation: true` — such a skill was in the model's list,
  so a repo's gate skill (/ship) could be run by the model itself. Codex's own policy holds: agents/openai.yaml
  `allow_implicit_invocation: false` — out of the list, an explicit skill input (the owner's /ship, hosts/codex_cmds)
  still works. A switched-off skill is not callable at all and a link to SKILL.md is lost (Codex takes the real path),
  so the session gets a copy of SKILL.md with the policy (its folder's other files linked or copied next to it) and the
  original is switched off;
- hooks: Claude Code runs a plugin's hooks and the repo's .claude/settings.json | settings.local.json hooks (gates of
  the repo). Codex runs hooks given in a thread's config (source session flags), next to the core's own hooks.json,
  only when the same config trusts each one (hooks.state.<key>.trusted_hash, the hash computed as Codex does it,
  hook_trust); matchers are Claude's ("*", "" and none — any tool, a plain name — that tool, otherwise a regex); the
  repos' hooks find their scripts by CLAUDE_PROJECT_DIR, which Codex does not set: it is put into the command's text
  and into app-server's environment, which a hook inherits. An edit in Codex is apply_patch: Codex runs a hook on
  "Edit|Write" for it (its aliases), but with the patch as tool_input.command — hooks/edit_hooks.py stands in front of
  such a hook and gives it Claude's Write/Edit calls of the patch (edit_groups, slice O);
- MCP of a plugin comes in the session's --mcp-config already (core/catalog.plugin_servers).
"""
import hashlib
import json
import os
import re
import shutil
import sys

# settings of the hub's Codex sessions: CLAUDE.md of the folder (and of the hub's hub-home) are instructions, like AGENTS.md;
# without the ChatGPT account's connectors (apps), plugins (the account's remote ones brought their skills into a
# session, and the catalog is a 100 MB clone into the folder) and Codex memory — the hub gives a session its tools;
# sign-in and MCP sign-ins are files in the hub's folder (not the system keychain)
CONFIG = """# Codex of the hub's sessions (hosts/codex.py) — the core rewrites this file on every start
project_doc_fallback_filenames = ["CLAUDE.md"]
cli_auth_credentials_store = "file"
mcp_oauth_credentials_store = "file"

[features]
multi_agent = false
apps = false
memories = false
plugins = false
remote_plugin = false
"""

# Codex features the isolation stands on, for a session's process — on its command line: a project's .codex/config.toml
# (Codex trusts the session's folder itself under danger-full-access) is a layer above config.toml, and in it
# `hooks = false` silently dropped the core's bans, `memories = true` put the memory summary into the prompt and ran its
# own model, `multi_agent = true` gave subagents (probe 2026-11-05). `-c`, not --enable/--disable: a flag the version
# does not know is a log line there, while --disable of an unknown one does not start app-server at all
PINNED = {"hooks": True, "multi_agent": False, "multi_agent_v2": False, "memories": False, "apps": False,
          "plugins": False, "remote_plugin": False, "external_agent_memory_import": False}

# The core's own model call (core/codexmodel.ask) has no tools at all: the core's hook refuses every one of them
# (hooks/deny_hint.py, HUB_CORE_MODEL=1), and the shell tool is also off in the thread itself — a second layer that does not
# depend on the hook (live 0.167.0, a fake Responses API: exec_command is gone from the model's tools and from code mode's
# `tools`; apply_patch and view_image stay — the hook refuses them, and the read-only sandbox holds writes). Only for that call: the critic
# and the evals run with the tools their work needs
CORE_MODEL_OFF = {"shell_tool": False}

# Codex's own skills a hub session does not get: skill-installer puts skills into $CODEX_HOME/skills — the hub's Codex
# folder, shared by all its sessions (one session's skill would reach every other); Claude has nothing like it. Off by
# name in the thread's config (live 0.162.1, a fake Responses API: it leaves the prompt, imagegen and the rest stay)
BUNDLED_OFF = ("skill-installer",)

# kept across starts: the hook's trust, and the Windows sandbox mode Codex's own setup wrote (hosts/codex_sandbox); project
# trust Codex adds again itself
KEEP_TABLES = ("[hooks.state.", "[windows]")


def write_config(home):
    """config.toml of the hub's Codex folder: CONFIG plus the hook's trusted_hash written after it — without it every
    hubd start re-trusted the hook through two app-server runs. Project trust tables are not kept: one per executor copy,
    they would pile up. Written only when it differs."""
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "config.toml")
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = ""
    kept = [b for b in re.split(r"\n(?=\[)", old) if b.lstrip().startswith(KEEP_TABLES)]
    text = CONFIG + "".join("\n" + b.strip() + "\n" for b in kept)
    if text != old:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(path + ".tmp", path)


REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_refs(value, env):
    """${NAME} / ${NAME:-default} from env in a string, a list or a dict's values, as Claude Code expands --mcp-config;
    an unknown name without a default stays as written."""
    if isinstance(value, str):
        return REF.sub(lambda m: env[m.group(1)] if m.group(1) in env else
                       (m.group(2) if m.group(2) is not None else m.group(0)), value)
    if isinstance(value, list):
        return [expand_refs(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_refs(v, env) for k, v in value.items()}
    return value


def setting_sources(flags):
    """The --setting-sources of claude's flags (the hub's setting_sources), ("project", "local") without one."""
    for i, f in enumerate(flags[:-1]):
        if f == "--setting-sources":
            return tuple(src.strip() for src in flags[i + 1].split(",") if src.strip())
    return ("project", "local")


def collect(flags, skill_roots, mcp, env=None, cwd=None, trust_at=None):
    """The session's set from claude's flags (the catalog) and its folder: plugins -> Codex skill roots, --mcp-config ->
    mcp_servers, the repo's .claude/skills -> skill roots; the result is `extra` for thread_config (hooks, skills off).
    The repo's skills and hooks follow the flags' --setting-sources, as claude's would (the hub's setting_sources).
    A server gets what claude gives it (probe 2026-11-05 with a real claude): ${NAME} and ${NAME:-default} expanded
    from the session's environment and that whole environment (env_vars — Codex passes a stdio server only HOME,
    PATH, USER and a few more plus its env: hub-browser lost HUB_STATE_DIR and took the default hub's browsers).
    trust_at — the path the hooks' trust keys start with (hook_trust); cwd=None — no repo."""
    env = {} if env is None else env
    x = lambda v: expand_refs(v, env)  # noqa: E731
    plugins, sources = [], setting_sources(flags)
    for i, f in enumerate(flags[:-1]):
        if f == "--plugin-dir":
            plugins.append(flags[i + 1])
            if os.path.isdir(os.path.join(flags[i + 1], "skills")):
                skill_roots.append(os.path.join(flags[i + 1], "skills"))
        elif f == "--mcp-config":
            try:
                with open(flags[i + 1], encoding="utf-8") as fh:
                    servers = (json.load(fh) or {}).get("mcpServers") or {}
            except (OSError, ValueError, AttributeError):
                continue
            for sname, s in servers.items():
                if not isinstance(s, dict):
                    continue
                if s.get("command"):
                    own = s.get("env") if isinstance(s.get("env"), dict) else {}
                    mcp[sname] = {"command": x(s["command"]), "args": x(list(s.get("args") or [])),
                                  **({"env": x(own)} if own else {}),
                                  **({"env_vars": sorted(n for n in env if n not in own)} if env else {})}
                elif s.get("url"):
                    mcp[sname] = {"url": x(s["url"]), **({"http_headers": x(s["headers"])} if s.get("headers") else {})}
    home = env.get("HOME") or env.get("USERPROFILE")
    repo = repo_skill_roots(cwd, home) if cwd and "project" in sources else []
    skill_roots[:] = list(dict.fromkeys(skill_roots + repo))   # the catalog gives hub-base too
    off, shadow = gate_skills(skill_roots, repo, _shadow_dir(env))
    if shadow:
        skill_roots.append(shadow)
    extra = {"skills": off} if off else {}
    hooks = edit_groups(session_hooks(plugins, cwd, sources, home))
    if hooks:
        extra["hooks"] = {**hooks, "state": {k: {"trusted_hash": v} for k, v in hook_trust(hooks, trust_at).items()}}
        if cwd:   # Claude Code gives it to a hook's environment, Codex does not; a hook inherits app-server's
            env["CLAUDE_PROJECT_DIR"] = cwd
    return extra


def user_skills_off(home):
    """[[skills.config]] entries that switch off the user's own skills, $HOME/.agents/skills (Codex's USER scope): a
    session of a hub on Claude does not get ~/.claude/skills either (--setting-sources project,local) — the hub gives a
    session its skills. Codex has no switch for the scope, only per SKILL.md; links are followed, as Codex does."""
    root = os.path.join(home or "", ".agents", "skills")
    if not home or not os.path.isdir(root):
        return []
    return [{"path": p, "enabled": False} for p in skill_files(root)]


def args(enabled=()):
    """The isolation pins on the session's app-server command line (PINNED); a feature the session turns on itself
    (`--enable`, a slice's own decision) is not pinned off."""
    return [x for f, on in PINNED.items() if on or f not in enabled
            for x in ("-c", f"features.{f}={'true' if on else 'false'}")]


def core_model_args():
    """The core's model call's extra pins on its app-server command line (CORE_MODEL_OFF)."""
    return [x for f, on in CORE_MODEL_OFF.items() for x in ("-c", f"features.{f}={'true' if on else 'false'}")]


def thread_config(mcp, env=None, hooks=None, skills=()):
    """The thread's config: the set's MCP servers; the user's own skills (user_skills_off of the session's HOME),
    BUNDLED_OFF and `skills` (collect: gate skills' originals, farther twins) off; `hooks` — collect's plugin and repo
    hooks with their trust."""
    # HOME, and on Windows the profile folder Codex takes as home there (HOME is usually unset)
    homes = [h for h in ((env or {}).get("HOME"), (env or {}).get("USERPROFILE")) if h]
    off = list({os.path.normcase(os.path.abspath(e["path"])): e for h in homes for e in user_skills_off(h)}.values())
    off += [{"name": n, "enabled": False} for n in BUNDLED_OFF] + list(skills)
    return {**({"mcp_servers": mcp} if mcp else {}), "skills": {"config": off}, **({"hooks": hooks} if hooks else {})}


# --- the repo's skills -------------------------------------------------------------------------------------------------
SHADOW = "codex-skills"   # <state>/sessions/<name>/: the session's copies of gate skills, rewritten on every start
# Codex's own "the model does not call it" (agents/openai.yaml next to SKILL.md); Claude's flag it ignores
POLICY = "policy:\n  allow_implicit_invocation: false\n"
GATE = re.compile(r"^disable[-_]?model[-_]?invocation\s*:\s*(.*)$", re.I | re.M)
NOT_SET = ("", "false", "no", "off", "0", "null", "~")


def _repo_root(d):
    """The nearest folder at or above d holding .git (a worktree's own root), or None."""
    while not os.path.exists(os.path.join(d, ".git")):
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up
    return d


def _homes(home):
    """The session's HOME and the hub's own: the user's scope, never read as the repo's."""
    return {os.path.normcase(os.path.realpath(h)) for h in (home, os.path.expanduser("~")) if h and h != "~"}


def repo_skill_roots(cwd, home=None):
    """<folder>/.claude/skills from the session folder up to its repo root, nearest first, never at or above HOME (the
    user's own scope; the hub's own home too, should the session's HOME be another) — as Claude Code 2.1.309 reads
    project skills (getProjectDirsUpToHome); a worktree with none of its own gets the main checkout's. Existing
    folders only."""
    from core.mcplogin import _git_root   # the repo's main checkout, as Claude Code keys its project by
    d = os.path.realpath(cwd)
    homes, root = _homes(home), _repo_root(d)
    found = []
    while os.path.normcase(d) not in homes:
        found.append(os.path.join(d, ".claude", "skills"))
        up = os.path.dirname(d)
        if d == root or up == d:
            break
        d = up
    found = [f for f in found if os.path.isdir(f)]
    main = _git_root(cwd) if root else None
    own, other = os.path.join(root or "", ".claude", "skills"), os.path.join(main or "", ".claude", "skills")
    if root and main != root and own not in found and os.path.normcase(main) not in homes and os.path.isdir(other):
        found.append(other)
    return found


def _is_gate(path):
    """SKILL.md says disable-model-invocation (any spelling Claude Code reads, any value but a plain false)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read(64 * 1024)
    except OSError:
        return False
    parts = re.split(r"^---[ \t]*$", text, maxsplit=2, flags=re.M)
    if len(parts) < 3 or parts[0].strip():
        return False
    m = GATE.search(parts[1])
    return bool(m) and re.sub(r"\s+#.*$", "", m.group(1)).strip().strip("'\"").lower() not in NOT_SET


def _shadow_dir(env):
    state, name = env.get("HUB_STATE_DIR"), env.get("HUB_SESSION")
    return os.path.join(state, "sessions", name, SHADOW) if state and name else None


DEPTH = 6   # Codex finds a SKILL.md up to six folders below a skill root, links followed (probe 2026-11-05)


SKIP = ("node_modules", ".git")   # a skill's tools: thousands of folders on every start, no skill of Claude's in them


def skill_files(root):
    """The SKILL.md files Codex finds under a skill root: up to DEPTH folders down, through links (each folder once —
    links may loop)."""
    found, seen = [], set()
    for d, dirs, files in os.walk(root, followlinks=True):
        try:
            st = os.stat(d)
        except OSError:
            dirs[:] = []
            continue
        if (st.st_dev, st.st_ino) in seen or d.count(os.sep) - root.count(os.sep) >= DEPTH:
            dirs[:] = []
        else:
            dirs[:] = [x for x in dirs if x not in SKIP]
        seen.add((st.st_dev, st.st_ino))
        if "SKILL.md" in files:
            found.append(os.path.join(d, "SKILL.md"))
    return sorted(found)


def _off(path):
    return {"path": os.path.realpath(path), "enabled": False}


def gate_skills(roots, repo, shadow):
    """(skills.config entries to switch off, the copies' root or None). What Claude Code reads of a skill root is
    <root>/<name>/SKILL.md; Codex finds deeper ones too — those are off. A gate skill (disable-model-invocation): its
    SKILL.md off, a copy with POLICY under `shadow` (none — the skill is only off); in the repo's roots a farther skill
    of a name the nearer root has is off (Claude Code: the nearest wins)."""
    off, seen, gates = [], set(), []
    for root in roots:
        for path in skill_files(root):
            n = os.path.relpath(os.path.dirname(path), root)
            if n == os.curdir or os.sep in n or (os.altsep and os.altsep in n) or (root in repo and n in seen):
                off.append(_off(path))
                continue
            if root in repo:
                seen.add(n)
            if _is_gate(path):
                off.append(_off(path))
                gates.append((n, os.path.dirname(path)))
    if not shadow:
        return off, None
    import platforms
    try:
        if os.path.lexists(shadow):
            platforms.current.rmtree(shadow)
        os.makedirs(shadow)
    except OSError as e:
        print(f"codex: the copies of the gate skills cannot be made ({e}) — the session goes without them", file=sys.stderr, flush=True)
        return off, None
    made = False
    for i, (n, src) in enumerate(gates):
        dst = os.path.join(shadow, str(i), n)   # the skill's own folder name: Codex names a skill without `name:` so
        try:
            _copy_gate(src, dst)
            made = True
        except OSError as e:   # the skill stays off: better no /ship than a session that does not start
            print(f"codex: skill {n}: the copy cannot be made ({e}) — the session goes without it", file=sys.stderr, flush=True)
            platforms.current.rmtree(os.path.dirname(dst))
    return off, (shadow if made else None)


def _copy_gate(src, dst):
    """SKILL.md as a file (a link Codex resolves to the original, losing the policy) and POLICY; the skill's other
    files next to it: folders linked (platforms.link_dir — a junction where Windows gives no links), files copied."""
    import platforms
    os.makedirs(os.path.join(dst, "agents"))
    with open(os.path.join(dst, "agents", "openai.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(POLICY)   # first: a copy left half made is without SKILL.md or with the policy, never a skill without it
    shutil.copyfile(os.path.join(src, "SKILL.md"), os.path.join(dst, "SKILL.md"))
    for entry in os.listdir(src):
        if entry in ("SKILL.md", "agents"):
            continue
        s, d = os.path.join(src, entry), os.path.join(dst, entry)
        try:
            if os.path.isdir(s):
                platforms.current.link_dir(s, d)
            else:
                shutil.copyfile(s, d)
        except OSError as e:
            print(f"codex: skill {os.path.basename(src)}: {entry} cannot be put next to the copy ({e})", file=sys.stderr,
                  flush=True)


# --- hooks: the plugins' and the repo's -------------------------------------------------------------------------------
# the events Codex knows (0.162.1, HookEventName): a thread config with another one would not start the thread
EVENTS = ("PreToolUse", "PermissionRequest", "PostToolUse", "PreCompact", "PostCompact", "SessionStart", "SessionEnd",
          "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop")
HOOK_TIMEOUT = 60    # Claude Code's default for a command hook, s (Codex's own is 600)
MAX_TIMEOUT = 24 * 3600   # a longer (or NaN, inf) one is taken as not given
SESSION_FLAGS = "/<session-flags>/config.toml"   # where Codex says a thread config's hooks come from (POSIX)


def _inside(root, rel):
    """The real path of rel inside root, or None (a hook file of a plugin only inside its folder, links resolved)."""
    full, root = os.path.realpath(os.path.join(root, rel)), os.path.realpath(root)
    try:
        return full if os.path.commonpath([full, root]) == root else None
    except ValueError:   # Windows: another drive
        return None


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _groups(data, wrap=lambda c: c):
    """{event: [{matcher?, hooks}]} of the events Codex has, command handlers only (a prompt or agent hook is a model
    call of Claude's), each with its timeout; anything malformed skipped."""
    out = {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    for event, groups in (hooks.items() if isinstance(hooks, dict) else ()):
        if event not in EVENTS or not isinstance(groups, list):
            continue
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                continue
            handlers = []
            for h in g["hooks"]:
                if not isinstance(h, dict) or h.get("type", "command") != "command" or not isinstance(h.get("command"), str):
                    continue
                t = h.get("timeout")
                ok = isinstance(t, (int, float)) and not isinstance(t, bool) and 1 <= t <= MAX_TIMEOUT
                handlers.append({"type": "command", "command": wrap(h["command"]),
                                 "timeout": int(t) if ok else HOOK_TIMEOUT,   # Codex reads a whole u64
                                 **({"async": True} if h.get("async") is True else {}),
                                 **({"statusMessage": h["statusMessage"]} if isinstance(h.get("statusMessage"), str)
                                    and h["statusMessage"] else {})})
            if handlers:
                m = g.get("matcher")
                out.setdefault(event, []).append({**({"matcher": m} if isinstance(m, str) else {}), "hooks": handlers})
    return out


# a path put into a command as it is: letters of any script, nothing a shell would read again ($( ), backticks, quotes,
# globs…); another path stays a reference (the environment has CLAUDE_PROJECT_DIR; a plugin root then does not reach
# the hook)
PLAIN = re.compile(r"[\w \-.,:/\\~+@=]+")


def _put_in(values):
    """A hook command with ${NAME} and $NAME of `values` put in as text: Codex runs a hook in the session's shell —
    PowerShell on Windows, where ${CLAUDE_PROJECT_DIR} is a PowerShell variable, not the environment's. Claude Code
    puts in CLAUDE_PLUGIN_ROOT, and both are in its hook's environment; Codex has CLAUDE_PLUGIN_ROOT there only for
    its own plugins (the feature is off), so a plugin script reading it from its environment does not find it."""
    refs = [(re.compile(r"\$(?:\{%s\}|%s(?![A-Za-z0-9_]))" % (n, n)), v) for n, v in values.items()
            if v and PLAIN.fullmatch(v)]
    def put(cmd):
        for ref, v in refs:
            cmd = ref.sub(lambda _: v, cmd)
        return cmd
    return put


def plugin_hooks(path, cwd=None):
    """A plugin's hooks as Claude Code reads them: hooks/hooks.json, then the manifest's `hooks` — a path, an inline
    object or a list of them; cwd — the session folder (CLAUDE_PROJECT_DIR)."""
    root = os.path.abspath(path)
    manifest = _read_json(os.path.join(root, ".claude-plugin", "plugin.json")) or {}
    found = []
    default = _inside(root, os.path.join("hooks", "hooks.json"))
    if default and os.path.isfile(default):
        found.append(_read_json(default))
    declared = manifest.get("hooks")
    for item in declared if isinstance(declared, list) else [declared] if declared else []:
        if isinstance(item, dict):
            found.append(item)
        elif isinstance(item, str):
            p = _inside(root, item)
            if p and p != default:
                found.append(_read_json(p))
    put = _put_in({"CLAUDE_PLUGIN_ROOT": root, "CLAUDE_PROJECT_DIR": cwd})
    return [_groups(d, put) for d in found if d]


def repo_hooks(cwd, sources=("project", "local"), home=None):
    """(the repo's hooks, all hooks off) as a Claude session of the hub reads them (Claude Code 2.1.309, of the
    --setting-sources given): project — <folder>/.claude/settings.json; local — .claude/settings.local.json of the
    repo's main checkout root and, when that is another folder, the folder's own. Never a HOME's: that is the user's
    own ~/.claude/settings.json, which a hub session does not get."""
    from core.mcplogin import _git_root
    homes = _homes(home)
    files = []
    if "project" in sources:
        files.append((cwd, "settings.json"))
    if "local" in sources:
        root = _git_root(cwd)
        files.append((root, "settings.local.json"))
        if os.path.normcase(root) != os.path.normcase(os.path.realpath(cwd)):
            files.append((cwd, "settings.local.json"))
    data = [d for d in (_read_json(os.path.join(f, ".claude", n)) for f, n in files
                        if os.path.normcase(os.path.realpath(f)) not in homes) if d]
    put = _put_in({"CLAUDE_PROJECT_DIR": cwd})
    return [_groups(d, put) for d in data], any(d.get("disableAllHooks") is True for d in data)


def session_hooks(plugins, cwd, sources=("project", "local"), home=None):
    """The session's hooks: the repo's, then each plugin's in the order of the set; one handler of an event and matcher
    once. None — when the repo switches hooks off or there are none."""
    sets, off = repo_hooks(cwd, sources, home) if cwd else ([], False)
    if off:
        return None
    for p in plugins:
        sets += plugin_hooks(p, cwd)
    out, seen = {}, set()
    for s in sets:
        for event, groups in s.items():
            for g in groups:
                hs = [h for h in g["hooks"] if (event, g.get("matcher"), h["command"]) not in seen]
                seen.update((event, g.get("matcher"), h["command"]) for h in hs)
                if hs:
                    out.setdefault(event, []).append({**g, "hooks": hs})
    return out or None


EDIT_HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "edit_hooks.py")
TOOL_EVENTS = ("PreToolUse", "PostToolUse")


def edit_groups(hooks):
    """The session's hooks with hooks/edit_hooks.py in front of each command Claude Code would run on an edit (its
    group's matcher takes Edit or Write): Codex runs such a group on apply_patch with the patch in tool_input.command,
    the script gives the command Claude's calls of it. A command is wrapped once with the matchers of all its edit
    groups (Claude runs one command once per call). A group of names (Edit|Write|Bash) keeps its other names as they
    were, and the command gets a group "apply_patch"; a command under every tool or a regex — the script on every call
    (Codex's regex is not Claude's: the script matches as Claude does; the command leaves its name groups then). A
    command the script cannot be put in front of (Windows: a path that is no plain word) — its groups as before, with a
    line in the log."""
    if not hooks:
        return hooks
    from hooks import edit_hooks as eh

    def edits(m):
        return any(eh.matches(m, t) for t in eh.EDIT_TOOLS)
    out = {}
    for event, groups in hooks.items():
        if event not in TOOL_EVENTS:
            out[event] = groups
            continue
        held = {}   # an edit command -> its handler (the longest timeout; async only when every one is), matchers,
        for g in groups:   # whether one of them is every tool | a regex
            if edits(g.get("matcher")):
                for h in g["hooks"]:
                    e = held.setdefault(h["command"], {"h": h, "matchers": [], "all": False})
                    e["matchers"].append(g.get("matcher"))
                    e["all"] = e["all"] or eh.names(g.get("matcher")) is None
                    e["h"] = {**e["h"], "timeout": max(e["h"]["timeout"], h["timeout"])}
                    if not h.get("async"):
                        e["h"].pop("async", None)
        for g in groups:   # the same command in a group of other tools: the wrapper on every call runs it for them too
            if not edits(g.get("matcher")):
                for h in g["hooks"]:
                    e = held.get(h["command"])
                    if e and e["all"]:
                        e["matchers"].append(g.get("matcher"))
                        e["h"] = {**e["h"], "timeout": max(e["h"]["timeout"], h["timeout"])}
                        if not h.get("async"):
                            e["h"].pop("async", None)
        wrapped = {c: _edit_hook(e["h"], e["matchers"]) for c, e in held.items()}
        taken = {c for c, e in held.items() if e["all"] and wrapped[c] is not None}
        result, placed = [], set()
        for g in groups:
            m = g.get("matcher")
            if not edits(m):
                keep = [h for h in g["hooks"] if h["command"] not in taken]
                if keep:
                    result.append({**g, "hooks": keep})
                continue
            rest = [n for n in eh.names(m) or () if n not in (*eh.EDIT_TOOLS, "apply_patch")]
            keep = [h for h in g["hooks"] if wrapped[h["command"]] is not None and not held[h["command"]]["all"]]
            if rest and keep:
                result.append({**g, "matcher": "|".join(rest), "hooks": keep})
            for h in g["hooks"]:
                c = h["command"]
                if wrapped[c] is None:
                    result.append({**g, "hooks": [h]})
                elif c not in placed:
                    placed.add(c)
                    result.append({"hooks": [wrapped[c]]} if held[c]["all"] else
                                  {"matcher": "apply_patch", "hooks": [wrapped[c]]})
        out[event] = result
    return out


def _edit_hook(h, matchers):
    """A handler running hooks/edit_hooks.py in front of h's command for these matchers, or None (no line for this
    OS)."""
    import platforms
    from hooks import edit_hooks
    hook = platforms.current.hook_command(sys.executable, EDIT_HOOK)
    words = [edit_hooks.b64(json.dumps(matchers)), edit_hooks.b64(h["command"]), str(h["timeout"])]
    if hook.get("args"):   # Claude Code's exec form (Windows): Codex runs a line
        from hosts.codex_rules import _hook_line
        line = _hook_line([hook["command"], *hook["args"], *words])
        if not line:
            print(f"codex: the repo edit hook cannot be put before `{h['command']}` (the path cannot be written as a line) — it "
                  "gets Codex's patch as is", file=sys.stderr, flush=True)
            return None
    else:
        line = " ".join([hook["command"], *words])
    return {**h, "command": line, "timeout": min(MAX_TIMEOUT, h["timeout"] * edit_hooks.RUNS + edit_hooks.MARGIN)}


def hook_trust(hooks, flags=None):
    """{key: trusted_hash} of hooks given in a thread's config, computed as Codex 0.162.1 does it: sha256 of the hook's
    identity as compact JSON with sorted keys (checked live against hooks/list, tests/test_codex_set.py); flags — the
    path the keys start with as this Codex reports it (codex_rules.flags_path: "C:\\<session-flags>\\config.toml" on
    Windows — not the drive of the session's folder nor of CODEX_HOME, live 2026-11-05), SESSION_FLAGS without one."""
    out, flags = {}, flags or SESSION_FLAGS
    for event, groups in hooks.items():
        snake = re.sub(r"(?<!^)([A-Z])", r"_\1", event).lower()
        for gi, group in enumerate(groups):
            for hi, h in enumerate(group["hooks"]):
                handler = {"async": bool(h.get("async")), "command": h["command"], "timeout": h.get("timeout", 600),
                           "type": "command", **({"statusMessage": h["statusMessage"]} if h.get("statusMessage") else {})}
                ident = {"event_name": snake, "hooks": [handler],
                         **({"matcher": group["matcher"]} if group.get("matcher") is not None else {})}
                text = json.dumps(ident, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                out[f"{flags}:{snake}:{gi}:{hi}"] = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return out
