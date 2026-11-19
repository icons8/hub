#!/usr/bin/env python3
"""Codex (experimental): a repo's or a plugin's hook on file edits (matcher Edit|Write) for a hub's Codex session.

With Claude Code a PreToolUse/PostToolUse hook on "Edit|Write" gets each edit in Claude's form: tool_name Write
{file_path, content} or Edit {file_path, old_string, new_string, replace_all}. Codex edits by apply_patch and does run
such a hook for it (its aliases of apply_patch are Edit and Write, probe 2026-11-05 on 0.162.1), but gives tool_name
"apply_patch" and the whole patch in tool_input.command: a guard reading tool_input.file_path found nothing and let
every edit through. hosts/codex_set.edit_groups puts this script in front of such a hook:
`<python> edit_hooks.py <matchers> <command> <timeout>` (a JSON list of the groups' matchers that hold the command —
Claude runs one command once per call — and the command, both base64url: a word both POSIX shells and PowerShell read
as itself).

- apply_patch: the patch is split into Claude's calls — Add File -> Write {file_path, content}; Update File -> an Edit
  per hunk (old_string: its context and removed lines, new_string: context and added lines); Update with Move to ->
  Write of the new path with the new content (or an Edit per hunk there, when the old file does not give it); Delete
  File and a moved file's old path — none: Claude deletes by Bash rm, an Edit|Write hook does not see it (the repo's
  Edit(...) permission rules do hold them, hooks/repo_rules). Paths are absolute, from the hook's cwd,
  as Claude gives them. The hook runs once per call whose tool the group's matcher takes (Claude Code 2.1.309's rule:
  `matches`), in Claude's shell (/bin/sh -c: platforms.current.hook_shell), with Claude's input fields.
- PreToolUse: exit 2 (stderr), permissionDecision deny, decision block, continue false — the patch is refused, the
  reasons (each with its file) go to the model; ask — refused too (Codex's PreToolUse takes only deny, as for the repo's
  permission rules, hooks/repo_rules.ASK); updatedInput — refused (a patch cannot be rewritten from an edited call).
  A refused file refuses the whole patch: apply_patch is one call.
- PostToolUse: only for the files the patch did change (Codex's "Success. Updated the following files:" list), with
  Claude's tool_response (filePath, content | oldString/newString; originalFile is not known — null); exit 2, decision
  block and additionalContext go to the model next to the patch's output (additionalContext: Codex's decision block
  would replace the output); a hook that changed the file itself (a formatter) — the model is told so, as Claude Code
  tells it.
- any other tool (the group's matcher is every tool or a regex — Codex runs it on every call): the hook gets Codex's
  input as is, when the matcher takes the tool, and its exit code and output go back unchanged — but a refusal on
  PreToolUse (exit 2) goes as a deny with exit 0, Claude's message: Codex on Windows let a hook's exit 2 through.

A failure of this script on PreToolUse is a refusal, not a pass (the guard would be silently off): a deny on stdout
with exit 0, the form Codex holds everywhere — on Windows a hook's exit 2 went through as a mere error (winstand,
2026-11-05: Codex there ran the patch a hook refused by exit 2).
"""
import base64
import json
import os
import re
import subprocess
import sys
import time

EDIT_TOOLS = ("Edit", "Write")   # Claude Code 2.1.309's edit tools: MultiEdit and NotebookEdit are not there (probe)
PLAIN = re.compile(r"[A-Za-z0-9_|, -]+\Z")   # Claude's matcher of tool names, not a regex (2.1.309, tool events)
TIMEOUT = 60                     # a run's time without one given, s (Claude Code's default)
RUNS = 10                        # the whole patch's time is RUNS runs (hosts/codex_set.edit_groups gives Codex that)
MARGIN = 5                       # Codex gives the script RUNS runs + MARGIN s (hosts/codex_set._edit_hook)
LONGEST = 24 * 3600 - MARGIN     # ...at most: Codex's limit of a hook (hosts/codex_set.MAX_TIMEOUT) less the margin
DONE = "Success. Updated the following files:"
ASK = ("Repo hook `{cmd}` asks for the owner's \"yes\" on this edit ({path}): {why}. The Codex hub cannot ask him with "
       "a card from a hook, so the patch was not applied. If you need it, ask the owner with a form what to do; do "
       "not work around it yourself")
REWRITE = ("Repo hook `{cmd}` wants to rewrite the edit of {path} (updatedInput) — not possible on Codex, the patch "
           "was not applied. Make the edit the way the hook requires")
LATE = ("The repo hooks did not manage to check all files of the patch within {s} s — the patch was not applied. "
        "Split it into smaller patches")
LATE_AFTER = "The repo hooks did not manage to check within {s} s after the edit: {paths}"
UNREAD = ("The repo edit hook could not parse this patch (not a single file) — the patch was not applied: the repo "
          "hooks would not have checked it. Write the patch in apply_patch format (*** Begin Patch, *** "
          "Add/Update/Delete File: …)")
FAILED = ("The repo edit hook failed ({}): before the edit the patch is not applied, after it the edit is not "
          "checked. Retry; if the refusal repeats, tell the hub.")
FORMATTED = ("PostToolUse hook modified {path} after your edit (likely a formatter). If your next patch to it targets "
             "a region the hook reformatted, read the file first.")


def b64(text):
    """A word for the command line: base64url without its "=" padding (a plain word on Windows, where the line is
    built of plain words only: hosts/codex_rules.PLAIN), "-" for an empty text (an empty word is lost in a shell)."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=") if text else "-"


def unb64(word):
    """b64's text back; a padded word (a line written before the padding went) is read too."""
    return "" if word == "-" else base64.urlsafe_b64decode((word + "=" * (-len(word) % 4)).encode("ascii")).decode("utf-8")


def names(matcher):
    """The exact names of a Claude matcher, or None — every tool or a regex (Claude Code 2.1.309, tool events): names
    are letters, digits, _, - and spaces split by | or ,."""
    if not matcher or matcher == "*" or not PLAIN.match(matcher):
        return None
    return [n.strip() for n in re.split(r"[|,]", matcher) if n.strip()]


def matches(matcher, tool):
    """Does a hook group's matcher take a tool, as Claude Code 2.1.309 decides (checked live): none, "" and "*" — every
    tool; names — exactly one of them; otherwise a regular expression searched in the name ("^Write$", ".*"); a broken
    one takes none."""
    if not matcher or matcher == "*":
        return True
    listed = names(matcher)
    if listed is not None:
        return tool in listed
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return False


def matches_any(matchers, tool):
    """One command under several groups runs once per call, when any of them takes the tool (Claude Code 2.1.309)."""
    return any(matches(m, tool) for m in matchers)


# --- the patch ---------------------------------------------------------------------------------------------------------
HEAD = re.compile(r"\*\*\* (Add File|Update File|Delete File|Move to): (.+?)[ \t]*\Z")


def parse(text):
    """Codex's patch -> [{"op": add|update|delete, "path", "lines" (add), "move", "hunks": [[(mark, text)]]}], leniently,
    as Codex reads it: lines split by \n only (with \r\n), an update's first hunk may come without @@, an empty line in
    a hunk is an empty context line; `@@ <text>` — the hunk's anchor, ("@", text) first in it."""
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    start = next((i + 1 for i, line in enumerate(lines) if line.strip() == "*** Begin Patch"), 0)
    ops, cur = [], None
    for line in lines[start:]:
        if line.strip() == "*** End Patch":
            break
        m = HEAD.match(line)
        if m:
            kind, path = m.groups()
            if kind == "Move to":
                if cur and cur["op"] == "update":
                    cur["move"] = path
                continue
            cur = {"op": {"Add File": "add", "Update File": "update", "Delete File": "delete"}[kind], "path": path,
                   "lines": [], "move": None, "hunks": []}
            ops.append(cur)
        elif cur is None:
            continue
        elif cur["op"] == "add":
            if line.startswith("+"):
                cur["lines"].append(line[1:])
        elif cur["op"] == "update":
            if line.startswith("@@"):
                anchor = line[2:].strip()
                if not cur["hunks"] or any(k != "@" for k, _ in cur["hunks"][-1]):
                    cur["hunks"].append([])   # `@@ class A` then `@@ def f():` — two anchors of one hunk
                cur["hunks"][-1] += [("@", anchor)] if anchor else []
            elif line.strip() == "*** End of File":
                continue
            elif line[:1] in (" ", "-", "+") or line == "":
                if not cur["hunks"]:
                    cur["hunks"].append([])
                cur["hunks"][-1].append((line[:1] or " ", line[1:]))
    return ops


def _old_new(hunk):
    return ("\n".join(t for k, t in hunk if k in (" ", "-")), "\n".join(t for k, t in hunk if k in (" ", "+")))


def _applied(path, hunks):
    """The file at path with the hunks applied in order, or None (no file, a hunk not found): whole lines, each hunk
    after the previous one and after its anchor line, as Codex seeks (without Codex's whitespace-tolerant tries — then
    None, and the caller gives Edits)."""
    text = _read(path)
    if text is None:
        return None
    lines, at = text.split("\n"), 0
    crlf = bool(lines) and lines[0].endswith("\r")
    bare = [line[:-1] if line.endswith("\r") else line for line in lines]
    for hunk in hunks:
        for anchor in (t for k, t in hunk if k == "@"):
            at = next((i + 1 for i in range(at, len(bare)) if bare[i].strip() == anchor), None)
            if at is None:
                return None
        old = [t for k, t in hunk if k in (" ", "-")]
        new = [t for k, t in hunk if k in (" ", "+")]
        if not old:
            return None
        i = next((i for i in range(at, len(bare) - len(old) + 1) if bare[i:i + len(old)] == old), None)
        if i is None:
            return None
        lines[i:i + len(old)] = [t + "\r" for t in new] if crlf else new
        bare[i:i + len(old)] = new
        at = i + len(new)
    return "\n".join(lines)


def _read(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def indented(text):
    """A file line with spaces before it: Codex trims them and applies it, while parse() reads such a line as the text of
    the file before it (an add's line, a hunk's context) — a guard would not see that file (release review 2026-11-09)."""
    return any(line[:1] in (" ", "\t") and HEAD.match(line.strip()) for line in text.split("\n"))


def calls(patch, cwd, after=False):
    """Claude's calls of a patch: [(tool, tool_input, the file's absolute path)]; after — the patch is applied (a moved
    file's new content is on disk at its new path). A hunk of anchors only gives no Edit."""
    out = []
    for op in parse(patch):
        path = os.path.normpath(os.path.join(cwd, op["path"]))
        op["hunks"] = [h for h in op["hunks"] if any(k != "@" for k, _ in h)]
        if op["op"] == "add":
            out.append(("Write", {"file_path": path, "content": "".join(t + "\n" for t in op["lines"])}, path))
        elif op["op"] == "update" and op["move"]:
            dest = os.path.normpath(os.path.join(cwd, op["move"]))
            content = _read(dest) if after else _applied(path, op["hunks"])
            if content is not None:
                out.append(("Write", {"file_path": dest, "content": content}, dest))
            else:
                out += [("Edit", {"file_path": dest, "old_string": o, "new_string": n, "replace_all": False}, dest)
                        for o, n in map(_old_new, op["hunks"])]
        elif op["op"] == "update":
            out += [("Edit", {"file_path": path, "old_string": o, "new_string": n, "replace_all": False}, path)
                    for o, n in map(_old_new, op["hunks"])]
    return out


def changed(response, cwd):
    """The absolute paths Codex's apply_patch output says it changed (A, M), or None — not a success output."""
    if not isinstance(response, str) or DONE not in response:
        return None
    tail = response.split(DONE, 1)[1]
    return {os.path.normpath(os.path.join(cwd, m.group(1).strip()))
            for m in re.finditer(r"^[AM] (.+)$", tail, re.M)}


def tool_response(tool, tool_input):
    """Claude's tool_response of a done edit (2.1.309's fields; originalFile and the patch are not known here)."""
    if tool == "Write":
        return {"type": "create", "filePath": tool_input["file_path"], "content": tool_input["content"],
                "structuredPatch": [], "originalFile": None, "userModified": False}
    return {"filePath": tool_input["file_path"], "oldString": tool_input["old_string"],
            "newString": tool_input["new_string"], "originalFile": None, "structuredPatch": [], "userModified": False,
            "replaceAll": False}


# --- running the hook --------------------------------------------------------------------------------------------------
TIMED_OUT = "timeout"

def run(command, payload, timeout):
    """The hook's command with payload on stdin, as Claude Code runs it -> (exit code, stdout, stderr); a command that
    does not start — None, a timeout — TIMED_OUT, with why: a non-blocking error, as Claude's. Output as bytes."""
    from platforms import current
    try:
        p = subprocess.Popen(current.hook_shell(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, **current.apart())
    except OSError as e:
        return None, b"", str(e).encode("utf-8")
    try:
        out, err = p.communicate(payload, timeout=max(timeout, 0.1))
    except subprocess.TimeoutExpired:
        current.kill_detached(p, hard=True)
        p.communicate()
        return TIMED_OUT, b"", f"timed out after {timeout:g} s".encode("utf-8")
    return p.returncode, out, err


def _json(out):
    try:
        got = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _text(err):
    return err.decode("utf-8", "replace").strip()


def pre_verdict(code, out, err, cmd, tool, path):
    """(a refusal text or None, an additional context or None) of one PreToolUse run, Claude's meaning."""
    if code == 2:
        return f"PreToolUse:{tool} hook error ({path}): [{cmd}]: {_text(err) or 'blocked'}", None
    if code != 0:
        return None, None
    got = _json(out)
    spec = got.get("hookSpecificOutput") if isinstance(got.get("hookSpecificOutput"), dict) else {}
    why = spec.get("permissionDecisionReason") or got.get("reason") or got.get("stopReason") or ""
    ctx = spec.get("additionalContext") if isinstance(spec.get("additionalContext"), str) else None
    decision = spec.get("permissionDecision")
    if decision == "deny" or got.get("decision") == "block":
        return f"PreToolUse:{tool} hook error ({path}): {why or 'blocked'}", ctx
    if decision == "ask":
        return ASK.format(cmd=cmd, path=path, why=why or "no reason given"), ctx
    if got.get("continue") is False:
        return f"PreToolUse:{tool} hook stopped ({path}): {why or 'continue: false'}", ctx
    if spec.get("updatedInput") is not None:
        return REWRITE.format(cmd=cmd, path=path), ctx
    return None, ctx


def post_verdict(code, out, err, cmd, tool):
    """(a blocking text or None, an additional context or None) of one PostToolUse run, Claude's meaning."""
    if code == 2:
        return f'PostToolUse:{tool} hook blocking error from command: "{cmd}": [{cmd}]: {_text(err)}', None
    if code != 0:
        return None, None
    got = _json(out)
    spec = got.get("hookSpecificOutput") if isinstance(got.get("hookSpecificOutput"), dict) else {}
    ctx = spec.get("additionalContext") if isinstance(spec.get("additionalContext"), str) and spec.get(
        "additionalContext") else None
    why = got.get("reason") or got.get("stopReason") or ""
    if got.get("decision") == "block" or got.get("continue") is False:
        return f'PostToolUse:{tool} hook blocking error from command: "{cmd}": {why or "blocked"}', ctx
    return None, ctx


def _digest(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def patch(hook, matchers, cmd, timeout):
    """apply_patch: the hook per Claude call of the patch -> Codex's output (a dict) or None. Before the patch, what is
    not read (no patch text, no file in it) is refused: a guard would not have seen it."""
    event, cwd = hook.get("hook_event_name"), hook.get("cwd") or os.getcwd()
    command = (hook.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not parse(command) or indented(command):
        if event != "PreToolUse":
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": UNREAD}}
    todo = [c for c in calls(command, cwd, after=event == "PostToolUse") if matches_any(matchers, c[0])]
    if event == "PostToolUse":
        done = changed(hook.get("tool_response"), cwd)
        if done is None:   # the patch failed: Claude runs PostToolUse only after a done edit
            return None
        todo = [c for c in todo if c[2] in done]
    base = {k: hook[k] for k in ("session_id", "transcript_path", "cwd", "permission_mode", "hook_event_name",
                                 "tool_use_id") if k in hook}
    budget = min(timeout * RUNS, LONGEST)
    end, blocks, contexts, late = time.monotonic() + budget, [], [], []
    for n, (tool, tool_input, path) in enumerate(todo):
        left = end - time.monotonic()
        if left <= 0.1:   # the floor of a run: past it a fast hook would never count as cut, the loop would outlive Codex's limit
            late = [c[2] for c in todo[n:]]
            break
        payload = {**base, "tool_name": tool, "tool_input": tool_input}
        if event == "PostToolUse":
            payload["tool_response"] = tool_response(tool, tool_input)
            before = _digest(path)
        given = min(timeout, left)
        code, out, err = run(cmd, json.dumps(payload, ensure_ascii=False).encode("utf-8"), given)
        if code == TIMED_OUT and given < timeout:   # cut by the patch's budget, not by the hook's own time
            late = [c[2] for c in todo[n:]]
            break
        if event == "PreToolUse":
            why, ctx = pre_verdict(code, out, err, cmd, tool, path)
        else:
            why, ctx = post_verdict(code, out, err, cmd, tool)
            if before is not None and _digest(path) != before:
                ctx = "\n".join(filter(None, [ctx, FORMATTED.format(path=path)]))
        blocks += [why] if why else []
        contexts += [ctx] if ctx else []
    if late and event == "PreToolUse":
        blocks.append(LATE.format(s=int(budget)))
    elif late:
        contexts.append(LATE_AFTER.format(s=int(budget), paths=", ".join(dict.fromkeys(late))))
    context = "\n".join(dict.fromkeys(contexts))
    if event == "PreToolUse":
        spec = {"hookEventName": "PreToolUse", **({"additionalContext": context} if context else {})}
        if blocks:
            spec.update(permissionDecision="deny", permissionDecisionReason="\n".join(blocks))
        return {"hookSpecificOutput": spec} if len(spec) > 1 else None
    # a block too goes as additionalContext: Codex's decision block puts the reason in place of the patch's output
    # (probe), and the model would not know the patch was applied; Claude adds it next to the result
    text = "\n".join(blocks + ([context] if context else []))
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}} if text else None


def _matchers(word):
    """The script's matchers word: a JSON list; a thread started before it took one matcher (or "-", none)."""
    try:
        got = json.loads(word)
    except ValueError:
        got = None
    if isinstance(got, list) and all(m is None or isinstance(m, str) for m in got):
        return got
    return [word or None]


def main(argv, raw):
    matchers, cmd = _matchers(unb64(argv[1])), unb64(argv[2])
    timeout = float(argv[3]) if len(argv) > 3 else TIMEOUT
    hook = json.loads(raw.decode("utf-8", "replace"))
    tool = hook.get("tool_name") or ""
    if tool != "apply_patch":   # another tool: the hook as Codex would run it, when Claude's matcher takes the tool
        if not matches_any(matchers, tool):
            return 0
        code, out, err = run(cmd, raw, timeout)
        sys.stderr.buffer.write(err)
        if code == 2 and hook.get("hook_event_name") == "PreToolUse":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason":
                                                     f"PreToolUse:{tool} hook error: [{cmd}]: {_text(err) or 'blocked'}"}}))
            return 0
        sys.stdout.buffer.write(out)
        return code if isinstance(code, int) else 1
    got = patch(hook, matchers, cmd, timeout)
    if got:
        print(json.dumps(got))   # \uXXXX: Codex's reading does not depend on this process's stdout encoding
    return 0


def entry(argv):
    """The exit code. A failure is a refusal: on PreToolUse (or an input not read) a deny Codex shows to the model, exit
    0; after the patch the reason in stderr, exit 2 (a non-blocking error there, as Claude's)."""
    raw = b""
    try:
        raw = sys.stdin.buffer.read()
        return main(argv, raw)
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 — a guard silently stopped is worse than a refused patch
        why = FAILED.format(f"{type(e).__name__}: {e}")
        sys.stderr.buffer.write(why.encode("utf-8") + b"\n")
        if _json(raw).get("hook_event_name") == "PostToolUse":
            return 2
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": why}}))
        return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # platforms
    sys.exit(entry(sys.argv))
