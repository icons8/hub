#!/usr/bin/env python3
"""Core hook: puts a fact into the spool and always exits 0. Says nothing to the model, with one exception: after context
compaction a direction's session gets its backlog (`hub direction resume`) as context, see compacted_context.

Connected through `claude --settings <state>/hooks-settings.json` only in sessions the core started
(the session name and the state directory are in HUB_SESSION and HUB_STATE_DIR).
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Claude Code wakes the session itself when its background task finishes: <task-notification> is not human input
NOTICE = re.compile(r"<(status|summary)>(.*?)</\1>", re.S)
OWNER_EXIT = {"prompt_input_exit", "logout"}
# Claude Code 2.1.309 cuts additionalContext at 8000 characters and 200 lines; the hook itself has 10 s (hosts/common.py):
# the context is ready within BUDGET seconds from the hook's start (the interpreter's own start is not counted) or not
# at all; a line longer than LINE_CHARS is clipped
CONTEXT_CHARS, CONTEXT_LINES, LINE_CHARS, BUDGET = 7800, 190, 1000, 7
COMPACTED = ("The context has just been compacted. You lead the direction {slug}: its rules and state are in the backlog, "
             "not in the compaction summary. Below is the output of `hub direction resume {slug}`, taken by the core; rely "
             "on it and on the backlog documents.")
CUT = ("The output is longer than fits here and is cut: before you go on, read the whole `hub direction resume {slug}` "
       "and the backlog documents:")


def translate(hook):
    """A Claude Code hook input -> (event type, data) or None."""
    name = hook.get("hook_event_name")
    sid = hook.get("session_id")
    if name == "SessionStart":
        source = hook.get("source")
        if source == "compact":
            return "session.compacted", {"session_id": sid}
        return "session.started", {"session_id": sid, "source": source}
    if name == "UserPromptSubmit":
        # here, not at load: main catches an import failure (the hook must not break the session, it writes hook-errors.log)
        from core.forms import LINE_MARKER, MARKER, ended_tasks   # [hub:mN] / [hub:mN<tail>] / [hub:mN from sender]
        from core.redact import mask_head   # known key forms, masked before the cut: the tail of a cut key is unrecognizable
        prompt = hook.get("prompt") or ""
        # the journal masks this type too (core/journal.MASKED); here the whole prompt is seen, not its first 200
        # characters (a notification about a background command quotes the command, token and all)
        notice = {k: mask_head(v.strip(), 300) for k, v in NOTICE.findall(prompt)} \
            if prompt.lstrip().startswith("<task-notification>") else None
        # the notification is written by Claude Code, not the core: labels in it come from background output, not our
        # own (7 such turns in the whole journal history, our own label in none of 5503 notifications); a message
        # glued after it is our own
        _, end, after = prompt.rpartition("</task-notification>")
        if notice is not None:
            # which tasks ended, block by block: the status is past the 200 characters of prompt_head, and a monitor's
            # event has none (core/forms.ended_tasks)
            notice["ended"] = ended_tasks(prompt)
        text = prompt if notice is None else after if end else ""
        # our own label is at the start of a line (for a command, after its arguments; a foreign one in the words is a
        # quote, core/forms.quote_labels); if there is none, the first anywhere, as before
        found = LINE_MARKER.search(text) or MARKER.search(text)
        # more labels at line starts: Claude Code glued several messages from stdin into one turn
        also = [m.group(1) for m in LINE_MARKER.finditer(text) if not found or m.group(1) != found.group(1)]
        return "turn.started", {"marker": found.group(1) if found else None, "prompt_head": mask_head(prompt, 200),
                                **({"also": also} if also else {}), **({"notice": notice} if notice is not None else {})}
    if name == "Stop":
        return "turn.stopped", {}
    if name == "SessionEnd":
        reason = hook.get("reason")
        if reason == "clear":
            return None  # SessionStart(clear) follows /clear
        return ("session.ended" if reason in OWNER_EXIT else "session.end_other"), {"reason": reason}
    return None


def compacted_context(session, state_dir, run=None, start=None):
    """The context for a session whose conversation was just compacted: a direction's session (one with a backlog,
    core/reducer task.backlog) gets `hub direction resume <slug>` within Claude Code's limit; anyone else, a failure or
    no answer within BUDGET seconds from start: None. 30.10: the direction skill tells the session to run resume after
    compaction, it did not, and the rules from the backlog documents fell out of its context."""
    import glob
    import time
    import platforms
    from core import journal
    start = time.monotonic() if start is None else start
    st = journal.load_state(state_dir)
    if session not in st["sessions"] and st["aliases"].get(f"was:{session}") in st["sessions"]:
        session = st["aliases"][f"was:{session}"]   # renamed: its process keeps the old name, as in reducer._apply
    sess = st["sessions"].get(session) or {}
    slug, left = sess.get("backlog"), BUDGET - (time.monotonic() - start)
    if not slug or sess.get("kind") == "hub" or left < 1:
        return None
    try:
        r = (run or platforms.current.run)(
            [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub"),
             "direction", "resume", slug], left, capture_output=True, stdin=subprocess.DEVNULL,
            **platforms.current.apart(), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except (subprocess.TimeoutExpired, OSError):
        return None
    full = r.stdout.decode("utf-8", "replace").strip()
    out = "\n".join(x if len(x) <= LINE_CHARS else x[:LINE_CHARS] + "…" for x in full.split("\n"))
    if r.returncode or not out:
        return None
    head = COMPACTED.format(slug=slug)
    whole = head + "\n\n" + out
    if out == full and len(whole) <= CONTEXT_CHARS and whole.count("\n") < CONTEXT_LINES:
        return whole
    # too long or a line clipped: the document paths go first (they are the rules), then as much of the output as fits
    from modules.directions import direction
    base = os.path.join(direction.path_of(slug), "backlog", "docs")
    docs = direction.numbered(glob.glob(os.path.join(glob.escape(base), "*.md")))
    paths = [f"  {p}" for p in docs[:20]] + ([f"  … and {len(docs) - 20} more in {base}"] if len(docs) > 20 else [])
    head = "\n".join([head, CUT.format(slug=slug), *paths])
    lines, size = [], len(head) + 2
    for line in out.split("\n"):
        if size + len(line) + 1 > CONTEXT_CHARS or len(lines) + head.count("\n") + 3 >= CONTEXT_LINES:
            break
        lines.append(line)
        size += len(line) + 1
    return head + "\n\n" + "\n".join(lines)


def main():
    import time
    start = time.monotonic()
    session, state_dir = os.environ.get("HUB_SESSION"), os.environ.get("HUB_STATE_DIR")
    if not session or not state_dir:
        return
    try:
        # Claude Code writes UTF-8; text stdin uses the locale encoding (cp1251 on Russian Windows, KOI8-R...): the label
        # "[hub:mN from ...]" was not recognised, and a Cyrillic letter or an emoji crashed the hook, so the turn event was lost
        hook = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except ValueError:
        return
    found = translate(hook)
    if found:
        from core import perms, spool
        perms.private()   # the fact and the spool are for the hub's user only (core/perms.py)
        spool.put(os.path.join(state_dir, "spool"), found[0], session, found[1], src="hook")
    if found and found[0] == "session.compacted":   # after the fact: a failure here must not lose it
        context = compacted_context(session, state_dir, start=start)
        if context:   # UTF-8 as is: \uXXXX escapes would make the output six times longer
            sys.stdout.buffer.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                       "additionalContext": context}},
                                               ensure_ascii=False).encode("utf-8"))


def context_main():
    """`hub_hook.py --compacted`: only the context after a compaction, on stdout as UTF-8, no fact to the spool — for a
    host whose session calls no hooks of the core (Codex app-server, hosts/codex_life.py); the session and the state
    folder are in HUB_SESSION and HUB_STATE_DIR, as for the hook."""
    import time
    start = time.monotonic()
    session, state_dir = os.environ.get("HUB_SESSION"), os.environ.get("HUB_STATE_DIR")
    context = compacted_context(session, state_dir, start=start) if session and state_dir else None
    if context:
        sys.stdout.buffer.write(context.encode("utf-8"))


if __name__ == "__main__":
    try:
        context_main() if sys.argv[1:] == ["--compacted"] else main()
    except Exception as e:  # the hook must not break the session
        try:
            with open(os.path.join(os.environ.get("HUB_STATE_DIR", "/tmp"), "hook-errors.log"), "a") as f:
                f.write(f"{e!r}\n")
        except OSError:
            pass
    sys.exit(0)
