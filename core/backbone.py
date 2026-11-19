"""Repo skills that only a human launches (`disable-model-invocation: true`): the work scaffold — /ship, /…-work —
and its neighbours. The model cannot call them; a session asks the core to enter the command for it, as a human would (`hub slash`).

Owner decision 2026-10-07: respect the repo's rules and always develop the way a human would. The case: a session went
through the scaffold's stages by hand, with gaps — it had nothing to invoke it with. The core names such skills with a
fact at session start; which of them is the scaffold is decided by the session from the repo's rules."""
import os
import re

FRONT = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
SLASH = re.compile(r"/([A-Za-z0-9][\w:.-]*)(?:\s|$)")
# Claude Code commands that cut off or replace the session itself: neither a session nor the hub enters them for itself or others
ENDS_SESSION = {"clear", "exit", "quit", "logout", "login", "resume", "continue"}
DESCRIPTION_MAX = 160
# built-in Claude Code commands, until the session names them itself: the list comes with init, and init only after the
# first input (07.10: "/effort" as the first message after start would get the marker as its argument)
BUILTINS = {"add-dir", "agents", "autocompact", "color", "compact", "config", "context", "cost", "effort", "fast", "focus",
            "goal", "mcp", "model", "output-style", "recap", "rename", "status", "usage", "version"}

RULE = ("The repo you work in has skills that only a human's input launches (you cannot call them yourself):\n"
        "{skills}\n"
        "If one of them is the repo's work scaffold (it runs a task in stages: /ship, /…-work), run work that edits code "
        "here through it, as a human would: `hub slash \"/<scaffold> <task or ticket>\"` and end the turn — the core will "
        "enter the command for you when the turn ends, and then you go through the scaffold's stages and gates. Do not go "
        "through the scaffold's stages by hand. An answer, an analysis or a question without code edits — without the "
        "scaffold.")
# for a mini-hub (core/minihub.py) — instead of RULE: its executor runs the scaffold. With RULE next to "delegate", the
# mini-hub entered the scaffold for itself, the scaffold opened a working copy, and the core rejected the mini-hub's edits (16.10). The name comes from the core: the model does not see the environment
MINIHUB_RULE = ("The repo you work in has skills that only a human's input launches:\n{skills}\n"
                "The repo's work scaffold from this list (it runs a task in stages: /ship, /…-work) is run by an executor: "
                "write \"through /<scaffold>\" into the `hub task run` brief; the executor's name is new (`{name}-<gist>`), "
                "not the agent's name; yourself — no `hub slash` of the scaffold, no EnterWorktree and no `--worktree`.")


def command(text):
    """The command name if the text is a Claude Code command (`/name …`), otherwise None. A path (`/home/x/…`) is not a command."""
    found = SLASH.match(str(text or "").lstrip())
    return found.group(1) if found else None


def _root(cwd):
    """Repo root (up to the first folder with .git — for a working copy it is a file) or the folder itself."""
    path = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        up = os.path.dirname(path)
        if up == path:
            return os.path.abspath(cwd)
        path = up


def _front(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            found = FRONT.match(f.read(8192))
    except OSError:
        return None
    if not found:
        return None
    out, key = {}, None
    for line in found.group(1).splitlines():
        if line.startswith((" ", "\t")):
            if key:   # continuation of a value: a `>-` / `|` block or a line wrap (07.10: "/i8-work — >-")
                out[key] = (out[key] + " " + line.strip()).strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            key = None
            continue
        key, value = key.strip(), value.strip()
        out[key] = "" if value[:1] in (">", "|") else value.strip("\"'")
    return out


def _repo_files(cwd):
    """(name, path, frontmatter) of the repo's skills and commands — in the session folder and at its repo root."""
    if not cwd or not os.path.isdir(cwd):
        return
    for base in dict.fromkeys((os.path.abspath(cwd), _root(cwd))):
        skills, commands = os.path.join(base, ".claude", "skills"), os.path.join(base, ".claude", "commands")
        files = [(n, os.path.join(skills, n, "SKILL.md")) for n in _list(skills)]
        files += [(n[:-3], os.path.join(commands, n)) for n in _list(commands) if n.endswith(".md")]
        for name, path in files:
            if os.path.isfile(path):
                meta = _front(path) or {}
                yield meta.get("name") or name, path, meta


def user_only(cwd):
    """[(name, description)] of repo skills and commands with disable-model-invocation: true — in the session folder and at its repo root."""
    found = {}
    for name, _, meta in _repo_files(cwd):
        if meta.get("disable-model-invocation", "").lower() == "true":
            about = " ".join(meta.get("description", "").split())
            found.setdefault(name, about[:DESCRIPTION_MAX] + ("…" if len(about) > DESCRIPTION_MAX else ""))
    return sorted(found.items())


def repo_skills(cwd):
    """{name: path of its file} of all the repo's skills and commands (core/kit.py: where a skill of the set comes from)."""
    found = {}
    for name, path, _ in _repo_files(cwd):
        found.setdefault(name, path)
    return found


def _list(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def note(cwd, minihub=None):
    """The rule for the session's system instructions, or None if the repo has no such skills; minihub — the mini-hub's
    name: it gets MINIHUB_RULE."""
    skills = user_only(cwd)
    if not skills:
        return None
    listed = "\n".join(f"- /{n}" + (f" — {a}" if a else "") for n, a in skills)
    return MINIHUB_RULE.format(skills=listed, name=minihub) if minihub else RULE.format(skills=listed)
