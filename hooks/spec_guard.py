#!/usr/bin/env python3
"""PreToolUse hook of an executor in spec mode (core/specmode.py; its own settings file, hosts/common.write_hook_settings
with spec=True, matcher on the file-editing tools only): while the spec is open the executor edits only the spec file —
Edit, Write, MultiEdit and NotebookEdit elsewhere are refused with the reason. Bash, MCP and the rest are not limited
(owner decision 2026-11-08: "a simple block at the level of the instruction is enough" — the line in its prompt holds
them). The mode comes from the projection the daemon writes (<state>/sessions/<name>/spec-mode.json) on every call: after
"Accept" (open: false) the edits are open at once, without a restart. A projection that cannot be read is a refusal;
no projection (the task is not recorded yet, or the session is not in spec mode) — the edit goes.
On a hub's Codex session the same block stands in the core's hook (hooks/deny_hint.py -> codex_reason, apply_patch)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import specmode   # noqa: E402

TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _guard(state_dir, name, cwd, targets):
    """None — the call goes; otherwise the refusal text. targets: the files the call writes — all of them must be the
    spec; none (a call that names no file, or one the hook cannot allow) refuses while the spec is open."""
    if not state_dir or not name:
        return specmode.UNREADABLE.format(why="no HUB_STATE_DIR or HUB_SESSION")
    try:
        got = specmode.read_projection(state_dir, name)
    except ValueError as e:
        return specmode.UNREADABLE.format(why=e)
    if not got or not got["open"]:
        return None
    spec = got["path"]
    try:
        if targets and not os.path.islink(spec) and all(
                isinstance(t, str) and t.strip() and _same(os.path.join(cwd or os.getcwd(), t), spec)
                for t in targets):
            return None
    except (ValueError, TypeError, OSError):   # a path or a cwd that cannot be resolved (a NUL, not a string): refused
        pass
    return specmode.REFUSAL.format(did=got.get("task"), path=spec, boss=got.get("boss") or "<whoever raised you>")


def reason(tool, tool_input, state_dir, name, cwd=None):
    """None — the call goes; otherwise the refusal text."""
    if tool not in TOOLS:
        return None
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return _guard(state_dir, name, cwd, [(tool_input or {}).get(key)])


def codex_reason(tool, tool_input, state_dir, name, cwd=None):
    """The same block on a Codex call (hooks/deny_hint.py calls it on every call). Codex edits only by apply_patch —
    tool_name apply_patch, the patch in tool_input.command: every file it adds or updates must be the spec, one other
    file refuses the whole patch (it is one call); Delete File and Move to are refused whatever their path (a move
    would carry the spec's text elsewhere, a delete would take the spec away); a patch with no file line the hook can
    read is refused too. Other tools — as with Claude: not limited. Claude never calls a tool by this name."""
    if tool != "apply_patch":
        return None
    from hooks.repo_rules import PATCH_FILE   # here, not at the top: Claude's hook of this file does not need it
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    files = PATCH_FILE.findall(command) if isinstance(command, str) else []
    targets = [path for _, path in files]
    if not files or any(kind not in ("Add File", "Update File") for kind, _ in files):
        targets = []   # no file the hook can read, or a delete or a move: refused whatever the path
    return _guard(state_dir, name, cwd if isinstance(cwd, str) else None, targets)


def main():
    try:
        hook = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        why = reason(hook.get("tool_name"), hook.get("tool_input") or {}, os.environ.get("HUB_STATE_DIR"),
                     os.environ.get("HUB_SESSION"), hook.get("cwd"))
    except (ValueError, AttributeError) as e:
        why = specmode.UNREADABLE.format(why=f"the hook's input: {e}")
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": why}}))


if __name__ == "__main__":
    main()
