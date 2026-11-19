"""Codex (experimental): evals/run.py on a hub on Codex (hub.json "host": "codex", or --host codex).

The temporary core of a run is a hub on Codex: its CODEX_HOME is <run>/state/codex-home (hosts/codex.CodexHost — the
state folder's codex-home), inside the run folder. The sign-in is the evals' own — <hub state folder>/codex-evals-home,
by a device code (`evals/run.py --codex-login`, the same code as `hub codex login`). Never the hub's sign-in
(<state>/codex-home/auth.json): its refresh token is one-time, a copy refreshed by a run would sign the hub out — and
never the owner's ~/.codex. A run gets a copy of the evals' auth.json; the copy, refreshed by the run's Codex, comes back
after the run; a lock keeps two runs from rotating one copy.

A case cut from history (cut_seq) continues the Codex thread of the session under test: a copy of its thread file
(rollout-…jsonl from <state>/codex-home/sessions), cut before the turn that took the case's message, goes into the run's
CODEX_HOME, and the session resumes it (thread/resume by its id). A case cut from a Claude Code conversation is refused —
a retelling of it would be a different conversation.
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.append(REPO)

HOME_NAME = "codex-evals-home"
NO_LOGIN = ("no Codex sign-in for the evals ({home}): sign in with a separate sign-in — evals/run.py --codex-login (a code "
            "on the phone); the evals never take the hub's sign-in — its token is one-time")
BUSY = ("another eval run on Codex holds the evals' sign-in ({home}): two runs would refresh one copy of the token — "
        "wait for it to end")
CLAUDE_CASE = ("{case}: the case was taken from a Claude conversation ({name}: {path}) — on Codex build the case again: "
               "cut_seq by the journal of a hub on Codex, transcripts.{name}.path — the session's thread file from "
               "<state>/codex-home/sessions")


def evals_home(state_dir):
    return os.path.join(state_dir, HOME_NAME)


def run_home(run):
    """The CODEX_HOME of the run's core: CodexHost takes <state folder>/codex-home."""
    return os.path.join(run, "state", "codex-home")


def thread_of(path):
    """The thread id of a Codex conversation file (rollout-…jsonl: the first line is session_meta) or None — a Claude
    Code conversation or not a conversation at all."""
    try:
        with open(path, encoding="utf-8") as f:
            first = json.loads(f.readline() or "null")
    except (OSError, ValueError):
        return None
    if isinstance(first, dict) and first.get("type") == "session_meta" and isinstance(first.get("payload"), dict):
        return first["payload"].get("id") or None
    return None


def _user_text(d):
    p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    if d.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
        return "\n".join(c.get("text") or "" for c in p.get("content") or [] if isinstance(c, dict))
    return None


def cut_rollout(src, marker, home):
    """The thread up to the turn where the session took the message labelled marker (the run feeds it again) — into
    home/sessions/YYYY/MM/DD under the same name, where Codex finds a thread by its id; -> (thread, lines kept). A
    message inserted into a running turn (turn/steer) — cut right before it: what the turn already did stays. The
    marker is whole and starts the message ("[hub:m3" is not "[hub:m30" nor a quote of it); no such message — a refusal, not the whole thread with its answer."""
    thread = thread_of(src)
    with open(src, encoding="utf-8") as f:
        lines = f.readlines()
    end, turn, answered = None, None, False
    whole = re.compile(r"^\s*" + re.escape(marker) + r"(?!\d)")   # the label the message starts with, not a quote of it
    for i, raw in enumerate(lines):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        text = _user_text(d)
        if text is not None and whole.search(text):
            end = i if answered or turn is None else turn
            break
        if d.get("type") == "event_msg" and p.get("type") == "task_started":
            turn, answered = i, False
        elif d.get("type") == "response_item" and not (p.get("type") == "message" and p.get("role") in ("user", "developer")):
            answered = True   # the model's output (a message, reasoning, a step): the turn is running
    if end is None:
        raise SystemExit(f"the thread {src} has no message with the marker {marker}")
    name = os.path.basename(src)
    day = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", name)
    dst = os.path.join(home, "sessions", *(day.groups() if day else ()), name)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.writelines(lines[:end])
    return thread, end


def _write_private(path, data):
    """bytes -> path, 0600, atomically (mkstemp creates the file 0600)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class Auth:
    """The evals' sign-in for the runs, held for the whole evals/run.py: give(run) — a copy of auth.json into the run's
    CODEX_HOME, take(run) — the copy back (Codex may have refreshed its token) and out of the run folder."""

    def __init__(self, state_dir):
        self.home = evals_home(state_dir)
        self.src = os.path.join(self.home, "auth.json")
        self.lock = None

    def __enter__(self):
        if not os.path.isfile(self.src):
            raise SystemExit(NO_LOGIN.format(home=self.home))
        self.lock = _lock(self.home)
        return self

    def __exit__(self, *exc):
        self.lock.close()

    def give(self, run):
        home = run_home(run)
        os.makedirs(home, exist_ok=True)
        with open(self.src, "rb") as f:
            _write_private(os.path.join(home, "auth.json"), f.read())

    def take(self, run):
        got = os.path.join(run_home(run), "auth.json")
        try:
            with open(got, "rb") as f:
                new = f.read()
        except OSError:
            return
        try:
            ok = isinstance(json.loads(new), dict)
        except ValueError:
            ok = False   # a half-written file: the evals' sign-in stays as it was
        with open(self.src, "rb") as f:
            old = f.read()
        if ok and new != old:
            _write_private(self.src, new)
        os.unlink(got)


def _lock(home):
    """The evals' sign-in lock: taken — refusal (another run is rotating the same copy)."""
    import platforms
    f = open(os.path.join(home, "run.lock"), "a")
    try:
        platforms.current.try_lock(f)
    except OSError:
        f.close()
        raise SystemExit(BUSY.format(home=home)) from None
    return f


def claude_conversations(case):
    """The case's conversations that are not Codex threads -> a refusal text, or None."""
    for name, t in (case.get("transcripts") or {}).items():
        if not thread_of(t["path"]):
            return CLAUDE_CASE.format(case=case.get("id"), name=name, path=t["path"])
    return None


def lost(events, start, target):
    """A Codex turn of the session under test refused for the sign-in or a limit after the fed message — a reason (n/a)."""
    for e in events:
        if e["seq"] < start:
            continue
        data = e.get("data") or {}
        if e["type"] == "codex.auth_lost" or (e["type"] == "turn.result" and e.get("entity") == target
                                               and data.get("auth_lost")):
            return "Codex is not signed in (the evals' sign-in: evals/run.py --codex-login)"
        if e["type"] == "turn.result" and e.get("entity") == target and data.get("limit"):
            return "Codex limit — the turn was cut"
    return None


# --- the evals' sign-in: evals/run.py --codex-login | --codex-status | --codex-logout ------------------------------
def login(state_dir):
    from core import codexcli
    home = evals_home(state_dir)
    os.makedirs(home, mode=0o700, exist_ok=True)
    lock = _lock(home)   # a run must not give its old copy back over a new sign-in

    def show(url, code):
        print(f"Open {url} and enter the code {code} (valid ~15 min); waiting for the confirmation…", flush=True)
    try:
        ok, email, plan, why = codexcli.login_flow(state_dir, show, home=home, logout="evals/run.py --codex-logout")
    except (OSError, RuntimeError) as e:
        ok, why = False, str(e)
    finally:
        lock.close()
    if not ok:
        print(f"not signed in: {why}")
        return 1
    print(f"Codex of the evals: signed in as {email}" + (f", plan {plan}" if plan else "") + f" ({home})")
    return 0


def status(state_dir):
    from core import codexcli
    home = evals_home(state_dir)
    if not os.path.isfile(os.path.join(home, "auth.json")):
        print(NO_LOGIN.format(home=home))
        return 1
    lock = _lock(home)   # reading the limits may refresh the token — not under a run's copy
    try:
        rpc = codexcli.Rpc(state_dir, home)
        try:
            got = codexcli.account(rpc)
            if not got:
                print(NO_LOGIN.format(home=home))
                return 1
            print(f"Codex of the evals: signed in as {got[0]}" + (f", plan {got[1]}" if got[1] else ""))
            try:
                print("limits: " + codexcli.windows_line(rpc.call("account/rateLimits/read", {})))
            except RuntimeError as e:
                print(f"limits: not read ({e})")
            return 0
        finally:
            rpc.close()
    except (OSError, RuntimeError) as e:
        print(f"Codex of the evals: {e}", file=sys.stderr)
        return 1
    finally:
        lock.close()


def logout(state_dir):
    from core import codexcli
    home = evals_home(state_dir)
    if not os.path.isdir(home):
        return 0
    lock = _lock(home)
    try:
        rpc = codexcli.Rpc(state_dir, home)
        try:
            rpc.call("account/logout", {})
        finally:
            rpc.close()
    except (OSError, RuntimeError) as e:
        print(f"Codex of the evals: {e}", file=sys.stderr)
        return 1
    finally:
        lock.close()
    return 0
