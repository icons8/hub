#!/usr/bin/env python3
"""PreToolUse hook of the bus's liaison only (core/liaison.py; its own settings file, hosts/common.write_hook_settings
with liaison=True): the liaison reads another hub's words, so its shell is closed down to `hub …` commands — one command,
no `;`, `&`, `|`, redirections, substitutions (`$`, backticks), backslashes or line breaks outside single quotes, no
unquoted `*`, `?`, `[`, `~` (file names); the form to the owner and the skill text stay open; files, code, the network,
subagents and MCP are refused. Deny rules (core/liaison.DENY, --disallowedTools) refuse the tools by name too; this
hook is what closes Bash, which deny cannot narrow to one program. Doubt means a refusal here: the liaison needs
nothing else (spec hubbus §5.13, the S1 probe)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.liaison import COMMANDS   # noqa: E402 — the same list bin/hub holds for the liaison

OPEN = ("Bash", "AskUserQuestion", "Skill", "ToolSearch")
WHY = ("The liaison may run only hub bus, hub chats and hub gist commands — one at a time, with no ;, &, |, redirections, "
       "$, \\, * ? [ ~ or backticks outside single quotes (the text goes in single quotes): hub bus "
       "send/pass/done/note/decline, hub bus contact ls, hub chats pass/send/done. Files, code, the network and executors "
       "are closed.")


def plain_hub(command, commands=COMMANDS):
    """One `hub …` command with nothing the shell would run besides it; commands — its subcommands allowed (None — any:
    hosts/codex_rules lets a read-only Codex session's hub commands through by the auto_allow setting)."""
    if not isinstance(command, str):
        return False
    text = command.strip()
    words = text.split()
    if len(words) < 2 or words[0] != "hub" or commands is not None and words[1] not in commands:
        return False
    quote = None
    for c in text:
        if quote == "'":
            if c == "'":
                quote = None
            continue
        if c in "\n\r`$\\":
            return False   # a line break, a substitution, an escape (`\"` keeps bash in the quotes) — also in "…"
        if quote == '"':
            if c == '"':
                quote = None
            continue
        if c in "'\"":
            quote = c
        elif c in ";&|<>(){}*?[~":
            return False   # separators, redirections, groups; * ? [ ~ — bash would expand them into file names
    return quote is None


def reason(tool, tool_input):
    if tool not in OPEN:
        return WHY
    if tool == "Bash" and not plain_hub((tool_input or {}).get("command")):
        return WHY
    return None


def codex_reason(tool, tool_input):
    """The same guard on a Codex call (hooks/deny_hint.py calls it for the liaison's session; probe 2026-11-05 on codex
    0.162.1: Codex runs the hook for each of its own tools — Bash, apply_patch, view_image, the goals,
    request_user_input — under these names). Open: the shell down to `hub …` and the owner's form (either of its two
    tools — request_user_input_async is what the 2026-10-19 pilot's model used)."""
    if tool in ("request_user_input", "request_user_input_async"):
        return None
    if tool == "Bash":
        return reason(tool, tool_input)
    return WHY


def main():
    try:
        hook = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        why = reason(hook.get("tool_name"), hook.get("tool_input") or {})
    except (ValueError, AttributeError):
        why = WHY
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": why}}))


if __name__ == "__main__":
    main()
