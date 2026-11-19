"""Plugin rights — what the owner approves (marketplaces spec §3.6, audit finding): an update that asks for more than
was approved is not installed without the owner's consent, the previous copy keeps working.

A right is a pair [kind, what exactly]: `page` (page code — full access to the page, whether `page_access` is declared
or not), `process` (the exact `run` command), `themes`, `skills` (a set of skills for sessions) and everything
executable in a set — it goes to Claude Code as a plugin: hooks (event, condition and the command itself), MCP and LSP
servers (name and the whole entry: command, arguments, address, headersHelper), monitors, `bin/` files (with a hash).
A key of the Claude Code plugin manifest that the hub does not know is also a right (`unknown`): closed until the hub
learns to show it. An extension is a pair that is not in the approved set: another command is another pair. A removed
right and new code with the same rights are not an extension.

The approved set is kept in the installed entry (`approved` in hub.json, written only by the hub). Entries installed
before that do not have it — then it is computed from what actually runs (`approved_of`): for an extension — from the
hub.json fields (`web`, `run`, `themes`), for a set — from the git objects of its clone's HEAD (not from working
files). Not computed — None: any update of such an entry only with consent.
"""
import hashlib
import json
import os
import re
import tarfile
import tempfile
from collections.abc import Mapping

import platforms
from core import codestore, langdata

KINDS = ("page", "process", "themes", "skills", "hook", "mcp", "lsp", "monitor", "bin", "external", "unknown", "script",
         "app", "shell")
_LABEL_KEYS = {"page": "core.plugrights.page", "process": "core.plugrights.process", "themes": "core.plugrights.themes",
               "skills": "core.plugrights.skills", "hook": "core.plugrights.hook", "mcp": "core.plugrights.mcp",
               "lsp": "core.plugrights.lsp", "monitor": "core.plugrights.monitor", "bin": "core.plugrights.bin",
               "external": "core.plugrights.external", "unknown": "core.plugrights.unknown",
               "script": "core.plugrights.script", "app": "core.plugrights.app", "shell": "core.plugrights.shell"}
# what of a plugin runs code (runs): a plugin with none of it is text only — its skills are given to sessions without the
# owner; with any of it — on his word (owner 2026-11-17: "the safe one automatically, installing on the owner's word")
RUNS = ("hook", "mcp", "lsp", "monitor", "bin", "external", "unknown", "script", "app", "shell")
# a program inside skills/: the model runs it with Bash — someone else's code like bin/ (executable bit, #! or these)
SCRIPT_EXT = (".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".ps1", ".bat", ".cmd")


class _Labels(Mapping):
    """kind -> the words for a human, in the language of the moment of the lookup (not of the import)."""

    def __getitem__(self, kind):
        from core import i18n
        return i18n.t(_LABEL_KEYS[kind])

    def __iter__(self):
        return iter(_LABEL_KEYS)

    def __len__(self):
        return len(_LABEL_KEYS)


LABELS = _Labels()
SET_FILE = os.path.join(".claude-plugin", "marketplace.json")
PLUGIN_FILE = os.path.join(".claude-plugin", "plugin.json")
CODEX_FILE = os.path.join(".codex-plugin", "plugin.json")
# keys every Codex plugin manifest has beyond Claude Code's (the screen card, ChatGPT apps — `app` in runs): known, not
# the `unknown` right — otherwise every Codex plugin would be risky
CODEX_KNOWN = {"interface", "apps"}
# keys of the Claude Code plugin manifest and of a set entry that the hub knows: the rest are the `unknown` right
KNOWN = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license", "keywords",
         "category", "tags", "strict", "source", "commands", "agents", "skills", "outputStyles", "themes",
         "syntaxHighlighting", "userConfig", "dependencies", "hooks", "mcpServers", "lspServers", "monitors"}
# executable: manifest key -> (kind of right, default file in the plugin root)
PARTS = {"hooks": ("hook", os.path.join("hooks", "hooks.json")), "mcpServers": ("mcp", ".mcp.json"),
         "lspServers": ("lsp", ".lsp.json"), "monitors": ("monitor", os.path.join("monitors", "monitors.json"))}
MAX_JSON = 1 << 20
# a file of a plugin that does not read is the `unknown` right "<what> is unreadable" (data: approvals keep it as is);
# an approval an earlier core recorded has it in the language of that core (language data) — the same right
UNREADABLE = " is unreadable"
_OLD_UNREADABLE = langdata.legacy("plugright_unreadable")


def _same(pair):
    """A right as it is compared: the earlier form of the unreadable file read as the current one."""
    kind, what = pair
    if kind == "unknown" and what.endswith(_OLD_UNREADABLE):
        return kind, what[:-len(_OLD_UNREADABLE)] + UNREADABLE
    return kind, what


def _exact(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(root, rel):
    """JSON of a file inside the plugin folder (after following links) or None; no file — False."""
    real, base = os.path.realpath(os.path.join(root, rel)), os.path.realpath(root)
    try:
        if os.path.commonpath([real, base]) != base:
            return None
    except ValueError:   # Windows: another drive
        return None
    if not os.path.lexists(os.path.join(root, rel)):
        return False
    try:
        if os.path.getsize(real) > MAX_JSON:
            return None
        with open(real, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _items(kind, data):
    """Entries of one part (hooks, servers, monitors) -> [what exactly]: the exact entry, so that any edit is visible."""
    if kind == "hook":
        hooks = data.get("hooks", data) if isinstance(data, dict) else None
        out = []
        for event, groups in (hooks or {}).items() if isinstance(hooks, dict) else []:
            for g in groups if isinstance(groups, list) else [groups]:
                inner = g.get("hooks") if isinstance(g, dict) else None
                matcher = (g.get("matcher") if isinstance(g, dict) else None) or "*"
                for h in inner if isinstance(inner, list) else [g]:
                    out.append(f"{event} [{matcher}] {_exact(h)}")
        return out
    if kind == "monitor":
        items = data.get("monitors", data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            return [f"{n}: {_exact(v)}" for n, v in items.items()]
        return [_exact(v) for v in items] if isinstance(items, list) else [_exact(items)]
    key = {"mcp": "mcpServers", "lsp": "lspServers"}[kind]
    servers = data.get(key, data) if isinstance(data, dict) else None
    return [f"{n}: {_exact(v)}" for n, v in servers.items()] if isinstance(servers, dict) else [_exact(data)]


def _plugin(root, label, entry):
    """Rights of one Claude Code plugin in a set: the executable from the manifest, the set entries and default files,
    and what Claude Code runs from its skills and commands (_skill_code)."""
    out = set()
    man = _load(root, PLUGIN_FILE)
    if man is None or (man is not False and not isinstance(man, dict)):   # unreadable — the hub does not know what is in it
        out.add(("unknown", f"{label}: {PLUGIN_FILE}{UNREADABLE}"))
    man = man if isinstance(man, dict) else {}
    entry = entry if isinstance(entry, dict) else {}
    for key in sorted((set(man) | set(entry)) - KNOWN):
        out.add(("unknown", f"{label}: {key}"))
    for key, (kind, default) in PARTS.items():
        found = []
        # Claude Code adds up the manifest parts and the set entry — both are counted
        declared = [x for src in (man, entry) if src.get(key) is not None
                    for x in (src[key] if isinstance(src[key], list) else [src[key]])]
        if key != "monitors" or not declared:   # monitors: the default file only if the manifest has none
            data = _load(root, default)
            if data is None:
                out.add(("unknown", f"{label}: {default}{UNREADABLE}"))
            elif data is not False:
                found += _items(kind, data)
        for item in declared:
            if isinstance(item, str):
                data = _load(root, item)
                if data is None or data is False:
                    out.add(("unknown", f"{label}: {key} {item}{UNREADABLE}"))
                    continue
                found += _items(kind, data)
            else:
                found += [_exact(item)] if kind == "monitor" else _items(kind, item)
        out |= {(kind, f"{label}: {x}") for x in found}
    cman = _load(root, CODEX_FILE)   # a Codex plugin: its manifest's parts and its hooks.json at the root count too
    if cman is None or (cman is not False and not isinstance(cman, dict)):
        out.add(("unknown", f"{label}: {CODEX_FILE}{UNREADABLE}"))
    elif cman:
        for key in sorted(set(cman) - KNOWN - CODEX_KNOWN):
            out.add(("unknown", f"{label}: {key}"))
        for key, default in (("hooks", "hooks.json"), ("mcpServers", None)):
            kind = PARTS[key][0]
            declared = cman.get(key) if isinstance(cman.get(key), list) else [cman[key]] if cman.get(key) else []
            for item in declared + ([default] if default and not declared and os.path.lexists(os.path.join(root, default))
                                    else []):
                data = _load(root, item) if isinstance(item, str) else item
                if data is None or data is False:
                    out.add(("unknown", f"{label}: {key} {item}{UNREADABLE}"))
                    continue
                out |= {(kind, f"{label}: {x}") for x in _items(kind, data)}
    bins = os.path.join(root, "bin")
    if os.path.isdir(bins):
        for d, names in codestore._walk(bins):   # including links to folders: their contents are programs too
            for n in names:
                p = os.path.join(d, n)
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                digest = os.readlink(p) if os.path.islink(p) else codestore._hash(p)
                out.add(("bin", f"{label}: {rel} {digest}"))
    return out | _skill_code(root, label, entry)


def _tree(root, cap=5000):
    """The files of a plugin folder -> (paths under root, more): links to folders followed (a personal skill is a link,
    core/mine.py), each real folder once; not bin/ and .git/ of the root (bin/ is its own right), not __pycache__. Past
    cap files the rest is not read: more is True."""
    out, seen, real = [], 0, set()
    for d, dirs, files in os.walk(root, followlinks=True):
        here = os.path.realpath(d)
        if here in real:
            dirs[:] = []
            continue
        real.add(here)
        dirs[:] = sorted(x for x in dirs if x != "__pycache__" and not (d == root and x in ("bin", ".git")))
        for n in sorted(files):
            seen += 1
            if seen > cap:
                return out, True
            out.append(os.path.join(d, n))
    return out, False


def _scripts(root, label, cap=5000, sealed=False):
    """Programs anywhere in the plugin (a skill tells the model to run `scripts/x.py` of its root as well as its own;
    bin/ is its own right) -> {("script", "<label>: <path>")}: the executable bit, a #! line or a script extension
    (files — _tree). Past cap files the rest is not read — that is `unknown`, not "text only". sealed — for an
    approval: all of them as one right with a hash of their contents (a rewritten script is a new right; a bundled
    node_modules is one line, not thousands)."""
    out, found = set(), []
    files, rest = _tree(root, cap)
    for p in files:
        hit = p.lower().endswith(SCRIPT_EXT)
        if not hit and not os.path.islink(p):
            try:
                hit = platforms.current.has_exec_bit(p)
                if not hit:
                    with open(p, "rb") as f:
                        hit = f.read(2) == b"#!"
            except OSError:
                hit = False
        if hit:
            found.append(p)
    rels = [os.path.relpath(p, root).replace(os.sep, "/") for p in found]
    if sealed and found:
        h = hashlib.sha1()
        for rel, p in sorted(zip(rels, found)):
            try:
                h.update(f"{rel}\0{codestore._hash(p)}\0".encode())
            except OSError:
                h.update(f"{rel}\0?\0".encode())
        out.add(("script", f"{label}: {len(found)} programs {h.hexdigest()[:12]}"))
    else:
        out |= {("script", f"{label}: {rel}") for rel in rels}
    return out | ({("unknown", f"{label}: more than {cap} files, the rest not read")} if rest else set())


def _skill_files(root, entry, cap=5000):
    """The files Claude Code reads as a skill or a command -> ([(name for the screen, text; None — unreadable)], more):
    every SKILL.md of _tree, every .md of the root's commands/ (any case of the letters: a Mac's disk does not tell them
    apart) and of the command paths of the manifest and the set entry — each folder walked on its own (a link to a
    folder _tree saw under another name, a path in bin/ or .git/), a named file whatever its extension, inline
    `content` of both. A path out of the plugin Claude Code does not load. Past cap files more is True: the rest is not
    read."""
    files, more = _tree(root, cap)
    want = [p for p in files if os.path.basename(p).lower() == "skill.md"]
    out, named = [], []
    try:
        named = [n for n in sorted(os.listdir(root)) if n.lower() == "commands"]
    except OSError:
        pass
    man = _load(root, PLUGIN_FILE)
    for where, src in ((PLUGIN_FILE.replace(os.sep, "/"), man), (SET_FILE.replace(os.sep, "/"), entry)):
        cmds = src.get("commands") if isinstance(src, dict) else None
        if isinstance(cmds, dict):
            for name, c in sorted(cmds.items()):
                if isinstance(c, dict) and isinstance(c.get("content"), str):
                    out.append((f"{where} commands.{name}", c["content"]))
                elif isinstance(c, dict) and isinstance(c.get("source"), str):
                    named.append(c["source"])
        else:
            named += [c for c in (cmds if isinstance(cmds, list) else [cmds]) if isinstance(c, str)]
    base = os.path.normpath(root)
    for c in named:
        at = os.path.normpath(os.path.join(root, c))
        if not at.startswith(base + os.sep):
            continue
        if os.path.isdir(at):
            got, over = _tree(at, cap)
            want += [p for p in got if p.lower().endswith(".md")]
            more = more or over
        elif os.path.lexists(at):
            want.append(at)
    for p in dict.fromkeys(os.path.normpath(p) for p in want):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        try:
            if os.path.getsize(p) > MAX_JSON:
                raise ValueError("too big")
            with open(p, encoding="utf-8", errors="replace") as f:
                out.append((rel, f.read()))
        except (OSError, ValueError):
            out.append((rel, None))
    return out, more


_BANG = re.compile(r"(?:^|(?<=\s))!`+([^`]+)`")   # "!`cmd`" at a line start or after whitespace (Claude Code's rule)
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*!")   # a "```!" block: its lines run
_AT = r"(?:^[ \t]*(?:-[ \t]+)?|:[ \t]+|[\[{,][ \t]*)"   # where a YAML key or value starts
# `hooks:` as a key: at any indent, in a list or a flow mapping, after a tag or an anchor, quoted
_HOOKS = re.compile(_AT + r"""(?:(?:!\S*|&\S+)[ \t]+)*["']?hooks["']?[ \t]*:""", re.M)
_WORD = re.compile(r"(?<![\w-])hooks(?![\w-])")
# what builds a key out of other text: an alias or an anchor (`&k hooks`, `*k :`), a tag, a merge, a complex key (`? `)
_INDIRECT = re.compile(_AT + r"(?:[*&]|!!?)[\w-]|<<|^[ \t]*\?(?:[ \t]|$)", re.M)
_ESCAPED = re.compile(r'"[^"\n]*\\[^"\n]*"[ \t]*:')   # a double-quoted key with an escape: "hoo\x6bs":


def _skill_code(root, label, entry=None, cap=5000):
    """What Claude Code itself runs from a plugin's skills and commands (code.claude.com/docs/en/skills): hooks in the
    frontmatter (registered when the skill is invoked) -> ("hook", "<label>: <file> <the whole frontmatter>"),
    "!`cmd`" and "```!" blocks (run before the content reaches the model) -> ("shell", "<label>: <file>: <command>").
    YAML is not parsed key by key — every form (an indent, a tag, an alias, a merge) would be a way around it: a
    frontmatter with `hooks:` where a key starts (any indent, a list, a flow mapping, a tag), an escaped key, or the
    word "hooks" with a form that builds a key out of other text (an alias, a merge, a tag, `? `) counts as hooks.
    Agents are not read: Claude Code ignores hooks in a plugin agent."""
    files, more = _skill_files(root, entry, cap)
    out = {("unknown", f"{label}: more than {cap} files, the rest not read")} if more else set()
    for rel, text in files:
        if text is None:
            out.add(("unknown", f"{label}: {rel}{UNREADABLE}"))
            continue
        lines = text.lstrip("\ufeff").replace("\r\n", "\n").split("\n")
        if lines[0].strip() == "---":   # Claude Code reads the frontmatter only from the first line, up to "---"
            # an indented "  ---" goes on a value; not closed — the whole file (reading more is the safe side)
            end = next((i for i, x in enumerate(lines[1:], 1) if x.rstrip() == "---"), len(lines))
            front = "\n".join(lines[1:end])
            if _HOOKS.search(front) or _ESCAPED.search(front) or (_INDIRECT.search(front) and _WORD.search(front)):
                out.add(("hook", f"{label}: {rel} {' '.join(front.split())}"))
        for m in _BANG.finditer("\n".join(lines)):   # the frontmatter too: reading less would be guessing
            out.add(("shell", f"{label}: {rel}: {' '.join(m.group(1).split())}"))
        fence = None
        for line in lines:
            if fence is None:
                m = _FENCE.match(line)
                if m:
                    fence, block = m.group(1), []
            elif line.strip().startswith(fence[0] * len(fence)) and not line.strip().strip(fence[0]):
                out.add(("shell", f"{label}: {rel}: {' ; '.join(x.strip() for x in block if x.strip())}"))
                fence = None
            else:
                block.append(line)
        if fence is not None:   # never closed: the rest of the file is the block
            out.add(("shell", f"{label}: {rel}: {' ; '.join(x.strip() for x in block if x.strip())}"))
    return out


def _apps(root, label):
    """ChatGPT apps of a Codex plugin (.app.json, the manifest's `apps`) -> {("app", "<label>: <name>")}: a connection of
    the owner's ChatGPT account to an outside service."""
    out = set()
    man = _load(root, os.path.join(".codex-plugin", "plugin.json"))
    where = [man["apps"]] if isinstance(man, dict) and isinstance(man.get("apps"), str) else []
    for rel in [".app.json", *where]:
        data = _load(root, rel)
        if data is None:
            out.add(("app", f"{label}: {rel}{UNREADABLE}"))
            continue
        apps = data.get("apps", data) if isinstance(data, dict) else None
        out |= {("app", f"{label}: {n}") for n in apps} if isinstance(apps, dict) else set()
    return out


def runs(root, label, entry=None, sealed=False, rights=None):
    """What of one plugin runs code -> a sorted list of pairs (RUNS): its executable rights (hooks, servers, monitors,
    bin/, unknown keys, what Claude Code runs from its skills), its programs and ChatGPT apps. Empty — text only. Not
    written into `approved` of what was installed before (scripts and apps are new kinds: an old installation would get
    "new rights" from them); a skill set entry approves them sealed (_scripts). rights — _plugin's result if the caller
    has it."""
    have = _plugin(root, label, entry) if rights is None else rights
    got = {p for p in have if p[0] in RUNS} | _scripts(root, label, sealed=sealed) | _apps(root, label)
    return sorted([k, v] for k, v in got)


def skills(root):
    """Rights of a skill set in folder root: the set itself and the executable of each of its plugins from the set
    folder. A set plugin with an external source (github, url…) is the `external` right: a set not from the hub's
    marketplace loads its copy, installed by Claude Code (for a marketplace set it is not loaded — core/catalog.set_tools,
    but the right is still visible)."""
    out = {("skills", "")}
    data = _load(root, SET_FILE)
    if not isinstance(data, dict):
        return out | ({("unknown", f"{SET_FILE}{UNREADABLE}")} if data is None else set())
    for p in data.get("plugins") or [] if isinstance(data.get("plugins"), list) else []:
        if not isinstance(p, dict):
            continue
        if not isinstance(p.get("source"), str):   # external source: a set not from the hub's marketplace loads it
            out.add(("external", f"{p.get('name')}: {_exact(p.get('source'))}"))
            continue
        path = os.path.normpath(os.path.join(root, p["source"]))
        if not (os.path.realpath(path) + os.sep).startswith(os.path.realpath(root) + os.sep) and \
                os.path.realpath(path) != os.path.realpath(root):
            continue
        out |= _plugin(path, str(p.get("name") or p["source"]), p)
    return out


def of(man, root=None):
    """Rights of a hub plugin by its manifest (market.valid) and its folder -> a sorted list of pairs."""
    out = set()
    if man.get("web"):
        out.add(("page", ""))
    run = man.get("run")
    if run:
        out.add(("process", _exact(list(run) if isinstance(run, list) else run)))
    if man.get("themes"):
        out.add(("themes", ""))
    if man.get("skills"):
        out |= skills(root) if root else {("skills", "")}
    return sorted([k, v] for k, v in out)


def _set_at(folder):
    """Rights of a set at its clone's HEAD — from git objects (git archive into a temp folder), not from working files.
    Not git or unreadable — None."""
    r = codestore.git("-C", folder, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    if r.returncode or not codestore.COMMIT.fullmatch(r.stdout.strip()):
        return None, None
    head = r.stdout.strip()
    tmp = tempfile.mkdtemp(prefix="hub-set-")
    try:
        archive = os.path.join(tmp, "set.tar")
        r = codestore.git("-C", folder, "archive", "--format=tar", f"--output={archive}", head)
        if r.returncode:
            return None, None
        with tarfile.open(archive) as tar:
            tar.extractall(os.path.join(tmp, "x"), filter="data")
        return skills(os.path.join(tmp, "x")), head
    except (tarfile.TarError, OSError):
        return None, None
    finally:
        codestore.remove(tmp)


def approved_of(ext=None, sets=()):
    """What is approved for an installation (its entries in extensions and sets) -> (list of pairs or None, set commit or None).
    A recorded `approved` is taken as is; for an entry without it — from what runs: the hub.json fields and the set clone's HEAD."""
    got, head = set(), None
    for x in ([ext] if ext else []) + list(sets):
        if "approved" not in x:
            continue
        if not isinstance(x["approved"], list):
            return None, None
        got |= {tuple(p) for p in x["approved"] if isinstance(p, list) and len(p) == 2
                and all(isinstance(s, str) for s in p)}
    if ext and "approved" not in ext:
        got |= {tuple(p) for p in of({"web": ext.get("web"), "run": ext.get("run"), "themes": ext.get("themes")})}
    for s in sets:
        if "approved" in s:
            continue
        path = s.get("path")
        found, head = _set_at(os.path.expanduser(path)) if isinstance(path, str) and os.path.isdir(
            os.path.expanduser(path)) else (None, None)
        if found is None:
            return None, None
        got |= found
    return sorted([k, v] for k, v in got), head


def wider(new, approved):
    """What the new version asks for beyond the approved -> a list of pairs; approved None (unknown) — everything new."""
    have = {_same(tuple(p)) for p in approved or []}
    return [p for p in new if approved is None or _same(tuple(p)) not in have]


def shown(right):
    """A right for the screen: "a process on the machine…: python3 main.py"."""
    kind, what = _same(tuple(right))
    if kind == "unknown" and what.endswith(UNREADABLE):
        from core import i18n
        what = i18n.t("core.plugrights.unreadable", what=what[:-len(UNREADABLE)])
    if kind == "process":
        try:
            what = " ".join(json.loads(what))
        except (ValueError, TypeError):
            pass
    if kind in ("hook", "mcp", "lsp", "monitor", "shell") and len(what) > 240:
        what = what[:240] + "…"
    return LABELS.get(kind, kind) + (f": {what}" if what else "")


def digest(rights):
    """A short fingerprint of the rights list (for the consent event)."""
    return hashlib.sha1(_exact(rights).encode()).hexdigest()[:12]
