"""Spec mode (owner decisions 2026-11-08). A person turns specs on for a session with /spec on in its composer
(session.spec_set, only from the page or Telegram); that session may raise an executor with `hub task run … --spec`.
The executor's task carries its spec — `{"path": "specs/<folder>/spec.md", "state": "open"}` — and while it is open:
- the executor writes only that file: a line in its brief and its prompt, and a hook on the file-editing tools
  (hooks/spec_guard.py, its own settings file); Bash, MCP and the rest are not limited (the owner: "a simple block at
  the level of the instruction is enough");
- its turn results are interim reports to whoever raised it: the task goes to the `spec` state of DELEGATION
  (core/machines.py), not `replied`; holds() keeps it awake, unparked and not removed, and no reminder wakes it;
- it ends only with a person's "Accept" (delegation.spec_accepted: the spec is copied to spec.accepted.md, the task
  is `working` again, the executor gets "do the work by it") or "Drop" (delegation.spec_dropped: the task is done, the
  file becomes spec.dropped.md, the executor sleeps); whoever raised it does that only with the owner's word (--for mN).
The hook learns the mode from a projection the daemon writes — <state>/sessions/<name>/spec-mode.json — so "Accept"
opens the edits at once, without a restart. No imports of the hub at the top: the hook loads this file too."""
import hashlib
import json
import os
import re
import shutil

DIR, FILE, ACCEPTED, DROPPED = "specs", "spec.md", "spec.accepted.md", "spec.dropped.md"
PROJECTION = "spec-mode.json"
FOLDER = re.compile(r"(?!d[0-9]+$)[a-z0-9][a-z0-9-]{0,39}")   # dN is the default folder of task dN — not given by hand
PENDING = re.compile(r"\.accepted-[0-9a-f]{12}(?:-[0-9a-f]{8})?\.md")   # the accepted copy before the core's yes (accept_copy)
HELD = ("working", "waiting-owner", "spec")


def rel_path(folder):
    return f"{DIR}/{folder}/{FILE}"


def folder_of(rel):
    return rel.split("/")[1]


def is_open(d):
    return isinstance((d or {}).get("spec"), dict) and d["spec"].get("state") == "open"


def holds(d):
    """The task holds its executor: in work, waiting for the owner, waiting for "Accept" (spec), or cut off with its
    spec still open (the person still has to accept or drop it)."""
    return d["state"] in HELD or (d["state"] == "stopped" and is_open(d))


def open_of(st, name):
    """The executor's task with an open spec, or None."""
    return open_by_session(st).get(name)


def direct(sess, task):
    """The open task of a live, awake executor — the one whose owner talks to it himself instead of through its mini-hub
    (owner decision 2026-11-09; a copy goes to the mini-hub); `task` is open_of / open_by_session of its name. A gone or
    sleeping executor (retired) is not talked to: its words go to whoever raised it, as before."""
    if sess and task and sess.get("state") not in ("removed", "closed", "failed", "retired"):
        return task
    return None


def awaiting(d, sess):
    """The spec waits for the person's decision right now — the one rule for the page's card, the strip in "Files" and
    Telegram's card (owner decision 2026-11-09: "while the discussion goes on, the card must not hang"): the task has
    reported (`spec`), and its executor is not in a turn — no turn running and no background job of its own. Their next
    word starts a turn, the card goes; the turn's result brings it back, once. `d` — the task, `sess` — its executor's row
    of the state (or the snapshot)."""
    if not d or d.get("state") != "spec":
        return False
    sess = sess or {}   # no row: nothing is writing it, the person may still accept or drop
    if sess.get("state") in ("busy", "starting", "dialog"):
        return False
    return not (sess.get("state") == "idle" and sess.get("background"))


def taken(st, folder):
    """Whose open spec lies in this folder — a task id or a queued executor's name — or None."""
    d = next((d for d in (st.get("delegations") or {}).values()
              if is_open(d) and folder_of(d["spec"]["path"]) == folder and d["state"] not in ("done", "archived")), None)
    if d:
        return d["id"]
    return next((n for n, r in (st.get("runs") or {}).items() if r.get("spec") == folder), None) \
        or next((s["name"] for s in (st.get("sessions") or {}).values() if s.get("task_spec") == folder
                 and s.get("state") not in ("removed", "closed", "failed")), None)


def open_by_session(st):
    """{executor: its task with an open spec}, one pass for a snapshot (views, hub status)."""
    out = {}
    for d in (st.get("delegations") or {}).values():
        if is_open(d) and d["state"] not in ("done", "archived"):
            out.setdefault(d.get("session"), d)
    return out


def projection(d):
    """What the hook reads about the executor of task d (spawn and the spec_mode action)."""
    return {"task": d["id"], "path": d["spec"]["path"], "boss": d.get("for_session"), "open": is_open(d)}


def in_brief(d):
    """The spec's path in the task's brief: the fold does not know the hub folder — the executor's environment does
    (HUB_STATE_DIR), and its system prompt carries the full path (hosts/streamjson.command)."""
    return f"$HUB_STATE_DIR/{d['spec']['path']}" if is_open(d) else None


def abs_path(state_dir, rel):
    return os.path.join(state_dir, *rel.split("/"))


def prepare(state_dir, rel):
    """The spec's folder and an empty file if there is none (one left from an earlier spec of the topic stays). A link on
    the way or in place of the file is refused: the hook compares real paths, and a link would let it write elsewhere."""
    path = abs_path(state_dir, rel)
    root = os.path.join(state_dir, DIR)
    for p in (root, os.path.dirname(path), path):
        if os.path.islink(p):
            raise OSError(f"{p} is a link")
    import platforms   # here, not at the top: the hook loads this file and needs no more than json and os
    platforms.current.makedirs_private(os.path.dirname(path))
    if not os.path.exists(path):
        with open(path, "a", encoding="utf-8"):
            pass
    return path


def write_projection(state_dir, name, spec):
    """<state>/sessions/<name>/spec-mode.json, atomically; an open spec gets its folder and file first."""
    path = prepare(state_dir, spec["path"]) if spec.get("open") else abs_path(state_dir, spec["path"])
    out = os.path.join(state_dir, "sessions", name, PROJECTION)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = f"{out}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"task": spec.get("task"), "path": path, "boss": spec.get("boss"), "open": bool(spec.get("open"))}, f)
    os.replace(tmp, out)
    return out


def read_projection(state_dir, name):
    """The projection or None if there is none; raises ValueError if it cannot be read (the hook refuses then)."""
    path = os.path.join(state_dir, "sessions", name, PROJECTION)
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from e
    if not isinstance(got, dict) or not isinstance(got.get("path"), str) or not isinstance(got.get("open"), bool):
        raise ValueError(f"{path}: broken")
    return got


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def accept_copy(state_dir, rel):
    """The person accepts the text as it is now: a copy of the file next to it under a pending name (before the fact: the
    fold does not read files); the daemon renames it to spec.accepted.md once the core accepts (the spec_mode action),
    whoever asked removes it on a refusal. -> (the pending copy's path relative to the state folder, the copy's sha256:
    the sum of what is accepted)."""
    path = abs_path(state_dir, rel)
    if os.path.islink(path) or not os.path.isfile(path):
        raise FileNotFoundError("the spec file is not there")
    tmp = abs_path(state_dir, rel.rsplit("/", 1)[0] + "/" + f".accepted-{os.urandom(10).hex()}.tmp")
    try:
        shutil.copyfile(path, tmp)   # the sum is taken from the copy, not from the file: an edit meanwhile cannot make them differ
        sha = digest(tmp)
        # its own name per request: two presses at once (the page and Telegram) do not share one file
        copy = rel.rsplit("/", 1)[0] + "/" + f".accepted-{sha[:12]}-{os.urandom(4).hex()}.md"
        os.replace(tmp, abs_path(state_dir, copy))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return copy, sha


def accepted_path(rel):
    return rel.rsplit("/", 1)[0] + "/" + ACCEPTED


def pending_ok(rel, pending):
    """The pending copy a fact names lies in its spec's folder under the pending form: the daemon renames nothing else."""
    return isinstance(pending, str) and pending.rsplit("/", 1)[0] == rel.rsplit("/", 1)[0] \
        and PENDING.fullmatch(pending.rsplit("/", 1)[-1]) is not None


def settle_copy(state_dir, rel, pending):
    """The core accepted: the pending copy becomes spec.accepted.md (a later accept in the same folder replaces it)."""
    if pending_ok(rel, pending) and os.path.isfile(abs_path(state_dir, pending)):
        os.replace(abs_path(state_dir, pending), abs_path(state_dir, accepted_path(rel)))


def drop_file(state_dir, rel):
    """A dropped spec is put aside, not deleted: spec.md -> spec.dropped.md."""
    path = abs_path(state_dir, rel)
    if os.path.isfile(path) and not os.path.islink(path):
        os.replace(path, os.path.join(os.path.dirname(path), DROPPED))


def request(state_dir, st, did, action, sha=None, channel="web", wait=5.0):
    """A person's "Accept" or "Drop" from the page, "Files" or Telegram: the same steps as `hub task spec` — the pending
    copy of the file as it is now, the fact, the core's decision. -> (status, {ok | queued | error}). A refusal removes the
    pending copy. `sha` is not read any more (a press accepts the current text); it stays for the callers of the previous
    release, which pass it positionally."""
    import uuid
    from core import journal, spool
    d = (st.get("delegations") or {}).get(did) or {}
    if action not in ("accept", "drop") or not is_open(d):
        return 409, {"error": "closed"}
    data = {"channel": channel, "owner": True, "req": uuid.uuid4().hex[:12]}
    if action == "accept":
        try:
            data["pending"], data["sha256"] = accept_copy(state_dir, d["spec"]["path"])
        except OSError as e:
            return 409, {"error": str(e)}
    path = os.path.join(state_dir, "journal.jsonl")
    start = journal.mark(path)
    spool.put(os.path.join(state_dir, "spool"), "delegation.spec_accepted" if action == "accept"
              else "delegation.spec_dropped", did, data, src=channel)
    seq, why = journal.decided(path, data["req"], wait, 1 << 20, start=start)
    if why:
        if data.get("pending"):
            try:
                os.remove(abs_path(state_dir, data["pending"]))
            except OSError:
                pass
        return 409, {"error": why}
    return 202, {"ok": True} if seq is not None else {"ok": True, "queued": True}


def card(state_dir, d):
    """What a person sees of a spec to accept it: {task, path (abs), sha256 of the file now, head of the file}."""
    path = abs_path(state_dir, d["spec"]["path"])
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        data = None
    return {"task": d["id"], "executor": d.get("session"), "path": path,
            "sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
            "empty": not data}


# --- texts to sessions (English, as all the core's texts to sessions) ---------------------------------------------

EXECUTOR_LINE = ("Spec mode (task {did}): the task is a spec, not the work. Read anything — code, the journal, the web; "
                 "write only `{path}` — the system as it will be (mermaid diagrams, cases, slices, risks, questions). Do "
                 "not edit other files, do not run builds or tests that write, do not commit or push until the owner "
                 "accepts — put probes into the spec under \"Probes after acceptance\". In mermaid labels no `;`. The "
                 "owner reads it in \"Files\" and comments lines: answer in the thread and edit the file. Your turn's "
                 "result goes to {boss} as an interim report — the task stays open until the owner accepts the spec; "
                 "then you continue in this conversation and do the work by it.")
BOSS_RULE = ("Specs are on for you (the owner's /spec on): a large task — a new feature, a redesign, a change for "
             "everyone, anything the owner should agree before code — raise the executor with --spec first: hub task run "
             "<name> \"<brief>\" --work risky --spec. A bug fix, a small edit, an answer — without it; the owner said "
             "\"do it now\" — without it. The spec waits for the owner's \"Accept\"; accept or drop it yourself only on "
             "his word: hub task spec accept|drop dN --for mN, where mN is his own message to you ([hub:mN from user]), not "
             "the executor's. While a spec is open, its forks and questions are not a card (AskUserQuestion) to the owner: "
             "the executor keeps them in the spec's decisions section and answers in its own feed, where the owner reads "
             "and replies; you only retell them in a line.")
REFUSAL = ("Spec mode (task {did}): write only {path}; code, runs and commits — after the owner accepts the spec. Put "
           "probes into the spec under \"Probes after acceptance\"; a question — hub send {boss} \"…\".")
UNREADABLE = ("Spec mode: the mode file cannot be read ({why}) — the edit is not run; tell whoever raised you: "
              "hub send <name> \"…\".")
