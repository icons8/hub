"""Common to hosts: session settings for `claude --settings`: the core's hooks and a ban on killing processes by pattern."""
import glob
import json
import os
import shutil
import sys

import platforms

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
# processes by pattern (2026-10-05: `pkill -f x -u <name>` on macOS killed everything with the user name in its command
# line, since options after the pattern are not parsed there; `kill $(pgrep -f ...)` is the same havoc): a session
# stops only its own pid. deny works in bypassPermissions and inside `$(...)`, `&&`; the full path is a separate rule.
# Our own PreToolUse hook refuses before deny and says how to do it right (hooks/deny_hint.py): a bare refusal did not
# explain itself to sessions, and they tried again (2026-10-16). The same hook gives the hints that have no deny:
# `sleep N` of 30 s or more in the foreground (sessions waited like that in the background); `xargs kill` and killing the
# hub's own pid (`kill`, os.kill from `python`): killing by the pid of your own process is allowed, so it is a hook only;
# on Windows killing by image name (`taskkill //F //IM python.exe` took down the whole hub, 2026-10-20) and a
# powershell/cmd script line the hook examines whole
DENY = [f"Bash({path}{tool}:*)" for tool in ("pkill", "killall", "pgrep") for path in ("", "/usr/bin/", "/bin/")]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_hook_settings(path, python=None, liaison=False, spec=False):
    """The core's hooks for four moments, the DENY ban and a refusal with a hint (hooks/deny_hint.py), nothing else.
    Python is the same as the core's (there is no `python3` on Windows). liaison — the settings of the bus's liaison
    only: its guard (hooks/liaison_guard.py) on every tool call closes the shell down to `hub …` commands. spec — an
    executor in spec mode (core/specmode.py): its guard (hooks/spec_guard.py) on the file-editing tools only. The refusal
    hook stands on every Bash call for all sessions; a mini-hub's shell writes into its folder (sed -i, >, tee) Claude Code
    refuses by the Edit rule with a bare "denied", and the same hook says what to do instead (executor)."""
    python = python or sys.executable
    cmd = platforms.current.hook_command(python, os.path.join(REPO, "hooks", "hub_hook.py"))
    hint = platforms.current.hook_command(python, os.path.join(REPO, "hooks", "deny_hint.py"))
    hooks = {ev: [{"hooks": [{**cmd, "timeout": 10}]}] for ev in HOOK_EVENTS}
    # one entry on every Bash call, no `if`: a command Claude Code did not parse goes to every entry with an `if` at
    # once (29 python processes on Windows, past the timeout under load, 2026-11-11); the hook itself ends at once
    # when no rule's word is in the call (hooks/deny_hint.quiet)
    hooks["PreToolUse"] = [{"matcher": "Bash", "hooks": [{**hint, "timeout": 10}]}]
    if liaison:
        guard = platforms.current.hook_command(python, os.path.join(REPO, "hooks", "liaison_guard.py"))
        hooks["PreToolUse"].insert(0, {"matcher": "*", "hooks": [{**guard, "timeout": 10}]})
    if spec:
        guard = platforms.current.hook_command(python, os.path.join(REPO, "hooks", "spec_guard.py"))
        hooks["PreToolUse"].insert(0, {"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                                       "hooks": [{**guard, "timeout": 10}]})
    # the PowerShell tool (Claude Code on Windows): Stop-Process -Name gets the same refusal, there are no `if` rules for it
    hooks["PreToolUse"].append({"matcher": "PowerShell", "hooks": [{**hint, "timeout": 10}]})
    settings = {"hooks": hooks, "permissions": {"deny": DENY}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=1)
    platforms.current.replace(tmp, path)
    return path


class Trees:
    """Background processes of a session (2026-10-16): Claude Code runs Bash commands in its own OS sessions, and a dev
    server or a test database outlives claude as an orphan. The tree marker (platforms.TREE_ENV) is "<name>@<hub folder>",
    one for the whole life of the session: processes started before a park or a hub restart are its too. The core
    stops them with the end_tree action when the session is gone for good (core/reducer._for_good), and with
    shutdown(final=True) on `hub stop`."""
    state_dir = None

    def spec_settings(self):
        """An executor in spec mode: the core's hooks plus its guard on the file edits (hooks/spec_guard.py)."""
        return write_hook_settings(os.path.join(self.state_dir, "hooks-settings-spec.json"), spec=True)

    def tree_tag(self, name):
        return f"{name}@{self.state_dir}"

    def end_tree(self, name, **_):
        n = platforms.current.end_tagged(lambda tag: tag == self.tree_tag(name))
        return f"ended {n}"

    def end_trees(self):
        """All sessions of this hub, parked and sleeping alike: `hub stop`, the hub leaves for good."""
        return platforms.current.end_tagged(lambda tag: tag.endswith(f"@{self.state_dir}"))


def resume_on_disk(name, cwd, session_id, resume):
    """--resume when the conversation is already in the folder's project directory, whatever the core thinks: it may
    not know the conversation began (a turn whose start hook was cut, 11.11), and --session-id with an existing
    conversation fails with "Session ID … is already in use" on every raise. -> resume"""
    from core.system import has_transcript
    if resume or not has_transcript(cwd, session_id):
        return resume
    print(f"{name}: conversation {session_id} is already on disk — --resume", file=sys.stderr, flush=True)
    return True


LOST_NOTE = ("Your previous conversation ({session_id}) is no longer on disk (deleted), so this is a new conversation with "
             "the same session id: you do not remember what was said in it. What you knew lives in files: your agent "
             "file and memory, the task records (`hub task show`), the board of your direction. Read them before you "
             "go on; do not guess what the old conversation said.")


def resume_lost(name, session_id, resume, claude_dir=None):
    """--resume with the conversation in no project folder (a disk cleanup deleted old transcripts): Claude Code
    fails with "No conversation found" on every raise, a crash loop no restart ends. A new conversation with the same
    --session-id instead (a leftover `<id>/` folder of subagents does not stop it). Found in any project folder — resume
    as before: Claude Code finds it there too, carry_transcript copies it. No projects folder at all: a cleanup deletes
    transcripts, not the folder, so the conversations live elsewhere (another config folder, a stand-in claude) and
    nothing is known to be lost — resume as asked. -> (resume, lost)"""
    if not resume or not session_id:
        return resume, False
    root = claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    if not os.path.isdir(os.path.join(root, "projects")):
        return resume, False
    if glob.glob(os.path.join(glob.escape(os.path.join(root, "projects")), "*", glob.escape(f"{session_id}.jsonl"))):
        return resume, False
    print(f"{name}: conversation {session_id} is not on disk — a new conversation", file=sys.stderr, flush=True)
    return False, True


def carry_transcript(cwd, session_id, claude_dir=None):
    """The conversation for `--resume` is in the project folder of the session's new folder. Claude Code looks for it
    there; in other folders not every version finds it (checked 2026-10-25 on a real claude: 2.1.218 does not find even
    the only copy; 2.1.296-2.1.309 find it while it is alone, but with two or more copies: "No conversation found" and
    a crash loop). If it is not in the slug of `cwd`, or it is older there than the freshest one in another folder (we
    returned to the old folder, the conversation went on in another), the freshest one is copied (the file and the
    `<id>` subfolder), not moved. Best effort: if it fails, startup goes on as it was, no crash. -> where it was copied from."""
    if not cwd or not session_id:
        return None
    from hosts import worktree
    root = claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    projects = os.path.join(root, "projects")
    name = f"{session_id}.jsonl"
    tmp = None
    try:
        dest = os.path.join(projects, worktree.slug(cwd))
        mine = os.path.join(dest, name)
        found = [os.path.join(projects, d) for d in os.listdir(projects)
                 if os.path.join(projects, d) != dest and os.path.isfile(os.path.join(projects, d, name))] \
            if os.path.isdir(projects) else []
        if not found:
            return None
        src = max(found, key=lambda d: os.path.getmtime(os.path.join(d, name)))
        if os.path.exists(mine) and os.path.getmtime(mine) >= os.path.getmtime(os.path.join(src, name)):
            return None
        os.makedirs(dest, exist_ok=True)
        if os.path.isdir(os.path.join(src, session_id)):
            shutil.copytree(os.path.join(src, session_id), os.path.join(dest, session_id), dirs_exist_ok=True)
        tmp = f"{mine}.part{os.getpid()}"   # the file goes last and whole: it marks a finished copy
        shutil.copy2(os.path.join(src, name), tmp)
        os.replace(tmp, mine)
        tmp = None
        return src
    except (OSError, shutil.Error) as e:   # disk full, foreign permissions, the file vanished between the check and the copy
        print(f"carry_transcript: {session_id} into {cwd!r} is not copied — {e!r}", file=sys.stderr, flush=True)
        return None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def transcript_entry(cwd, session_id, uuid, claude_dir=None):
    """The entry of the conversation `session_id` in the folder's project directory whose `uuid` is this, as a dict, or
    None (no file, no such entry, unreadable). Searched from the end: the entries asked for are of the last turns, and a
    conversation of months is a gigabyte. -> dict | None"""
    if not (cwd and session_id and isinstance(uuid, str) and uuid):
        return None
    import mmap
    from core.system import transcript_path
    path = transcript_path(cwd, session_id, claude_dir)
    key = json.dumps({"uuid": uuid}, separators=(",", ":"))[1:-1].encode()   # "uuid":"…" as Claude Code writes it
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            at = m.rfind(key)
            while at != -1:
                start, end = m.rfind(b"\n", 0, at) + 1, m.find(b"\n", at)
                try:
                    row = json.loads(m[start:end if end != -1 else len(m)])
                except ValueError:
                    row = None
                if isinstance(row, dict) and row.get("uuid") == uuid:   # not the same text inside another entry
                    return row
                at = m.rfind(key, 0, start)
    except (OSError, ValueError):   # no file, an empty one (mmap), foreign permissions
        return None
    return None


def existing_cwd(cwd):
    """The session folder is gone: don't fail on spawn (a fork, a revive after a fork's auto-retire: the
    working copy and branch are already removed by `hosts/worktree.remove`). A working copy: go up to its repository
    (`<repo>/.claude/worktrees/<name>` -> `<repo>`, an instance's `<repo>/.claude/worktrees/@<instance>/<name>` too); if
    that is not found either, the home folder, as always without cwd.
    Fallback path: `hub session revive` first puts the working copy back (`hosts/worktree.restore`)."""
    if not cwd:
        return os.path.expanduser("~")
    if os.path.isdir(cwd):
        return cwd
    parent, tail = os.path.split(os.path.normpath(cwd))
    if os.path.basename(parent).startswith("@"):   # several hubs on one machine: an instance's namespace
        parent = os.path.dirname(parent)
    if tail and os.path.basename(parent) == "worktrees" and os.path.basename(os.path.dirname(parent)) == ".claude":
        repo = os.path.dirname(os.path.dirname(parent))
        if os.path.isdir(repo):
            return repo
    return os.path.expanduser("~")
