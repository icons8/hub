"""Steps of a helper agent (owner decision 2026-10-06: when an agent edits code, we see none of it).

A session calls Agent — for the hub that is one step, and the edits inside it do not reach the journal. Claude Code
writes the agent's conversation next to the session's conversation: ~/.claude/projects/<project>/<session_id>/subagents/agent-<id>.jsonl,
and agent-<id>.meta.json holds the toolUseId of the Agent step that called it. The page reads it on click; the journal is not touched.
"""
import glob
import json
import os
import re

from core import i18n, journal

ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
CLIP = 20000   # a step's input (code in Edit/Write) — shown as an edit in the feed; longer — truncate
RESULT = 4000


def project_dir(claude_dir, cwd):
    """The Claude Code project folder — by the real path of the session folder (hosts/worktree.slug), as claude names it:
    a folder reached through a link (/tmp on macOS) is found."""
    from hosts import worktree
    return os.path.join(claude_dir, "projects", worktree.slug(cwd) if cwd else "")


def _clip(v, n=CLIP):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + "\n" + i18n.t("core.subagents.clipped", count=len(v) - n)
    if isinstance(v, dict):
        return {k: _clip(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_clip(x, n) for x in v[:200]]
    return v


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def find(claude_dir, cwd, session_id, tool_id):
    """The conversation file of the agent called by step tool_id: first under the session's conversation, then across the
    whole project (the conversation continued with a new id — the agent lies under the old one). Not found or an id that
    does not look like an id — None."""
    if not (ID.match(str(session_id or "")) and ID.match(str(tool_id or ""))):
        return None
    root = project_dir(claude_dir, cwd)
    for pattern in (os.path.join(root, session_id, "subagents", "*.meta.json"),
                    os.path.join(root, "*", "subagents", "*.meta.json")):
        for meta in glob.glob(pattern):
            try:
                with open(meta, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(m, dict) and m.get("toolUseId") == tool_id:
                return meta[: -len(".meta.json")] + ".jsonl", m
    return None


_codex_seen = {}   # (state folder, session id) -> its codex-threads.json


def codex_steps(session_id, tool_id, state_dir=None):
    """Codex (experimental): a Codex subagent's steps — the codex host keeps them in the session's folder,
    sessions/<name>/codex-agents/<step>.json (hosts/codex_items.py), the session found by its id in codex-threads.json.
    Not a Codex session (a hub on Claude has no such files) — None."""
    if not (ID.match(str(session_id or "")) and ID.match(str(tool_id or ""))):
        return None
    if state_dir is None:
        from core.daemon import default_state_dir
        state_dir = default_state_dir()
    known = _codex_seen.get((state_dir, session_id))
    pattern = os.path.join(glob.escape(state_dir), "sessions", "*", "codex-threads.json")
    # the page asks every 3 s while the steps are open: the session's folder is remembered, not searched each time
    for threads in ([known] if known and os.path.exists(known) else []) + glob.glob(pattern):
        try:
            with open(threads, encoding="utf-8") as f:
                ours = session_id in json.load(f)
            if not ours:
                if threads == known:
                    _codex_seen.pop((state_dir, session_id), None)
                continue
            if len(_codex_seen) > 1000:
                _codex_seen.clear()
            _codex_seen[(state_dir, session_id)] = threads
            own = os.path.join(os.path.dirname(threads), "codex-agents", re.sub(r"\W", "_", tool_id) + ".json")
            with open(own, encoding="utf-8") as f:
                got = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(got, dict) and isinstance(got.get("items"), list):
            return {"agent": got.get("agent") or {}, "items": got["items"]}
    return None


def steps(claude_dir, cwd, session_id, tool_id, state_dir=None):
    """{agent: {type, description}, items: [{kind: said, text} | {kind: tool, id, name, input, result, status}]}."""
    found = find(claude_dir, cwd, session_id, tool_id)
    if not found:
        return codex_steps(session_id, tool_id, state_dir)
    path, meta = found
    items, tools = [], {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    for raw in lines:
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        content = (m.get("message") or {}).get("content") if isinstance(m, dict) else None
        if m.get("type") == "assistant" and isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == tool_id for b in content):
            # a fork (Claude Code 2.1.302) starts its conversation with the parent's inherited turn containing the call to
            # itself: everything before it and the turn itself are not its steps (replies to its calls below attach to
            # nothing). An ordinary agent does not see its own step in the conversation
            items, tools = [], {}
            continue
        if m.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if b.get("type") == "text" and b.get("text", "").strip():
                    items.append({"kind": "said", "text": _clip(b["text"], journal.TEXT_MAX)})
                elif b.get("type") == "tool_use":
                    item = {"kind": "tool", "id": b.get("id"), "name": b.get("name"), "input": _clip(b.get("input") or {}),
                            "status": "running"}
                    tools[b.get("id")] = item
                    items.append(item)
        elif m.get("type") == "user" and isinstance(content, list):
            for b in content:
                item = tools.get(b.get("tool_use_id")) if b.get("type") == "tool_result" else None
                if item:
                    item["result"] = _clip(_text(b.get("content")), RESULT)
                    item["status"] = "error" if b.get("is_error") else "done"
    return {"agent": {"type": meta.get("agentType"), "description": meta.get("description")}, "items": items}
