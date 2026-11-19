#!/usr/bin/env python3
"""Evals of the behaviour of the hub and its sessions, on the core journal.

  evals/run.py <case.json>… [--before REV] [--after REV] [--trials K] [--only before|after]

A case is a path or just a file name: evals/cases/ and evals/locale/ru/cases/ (the cases of a Russian-speaking owner, not
published) are both looked in.

A case is a moment of real history just before a mistake: a prefix of the core journal (events before `cut_seq`) and the
conversation of the session under test up to the message it answered wrongly. Or an invented history right in the case (`events`
instead of `cut_seq`, with no conversation: the session starts from a clean slate). The run brings up a temporary core on the
"before the fix" and "after" code versions (git worktree); the session under test is a real claude and continues from the same
place; checks are by the facts in the journal after the cut (the tool issued, a question to the owner, the text to the owner).
The live core and live conversations are not touched.

A case with `events` carries its own settings (`config`) — the machine's journal and hub.json are not read, so it runs on any
machine with a claude login; `$work` in it is the working folder inside the run (session folders). A case with `cut_seq` runs
on the machine where the hub lives: there are the journal (by default this hub's journal), its hub.json, the conversations and the claude login.

Codex (experimental): on a hub on Codex (hub.json "host": "codex", or --host codex) the temporary core is a hub on
Codex too and the session under test is a real Codex (CodexHost) with its own CODEX_HOME inside the run folder, on the
evals' own sign-in (`evals/run.py --codex-login`, a device code; --codex-status — the sign-in and the limits). A cut_seq
case continues the session's Codex thread (evals/codexrun.py).

  evals/run.py <case.json>… [--host codex] [--codex-model M] [--codex-effort E]
  evals/run.py --codex-login | --codex-status | --codex-logout
"""
import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
from core import catalog, journal  # noqa: E402
from core.daemon import default_state_dir  # noqa: E402

sys.path.insert(0, HERE)
from common import LOGGED_OUT, WORK, find_case, guard_codex, on_codex  # noqa: E402,F401 — shared by both eval runs
import codexrun  # noqa: E402


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def worktree(rev):
    """The code of version rev in its own folder (once per revision)."""
    sha = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", rev], capture_output=True, text=True,
                         check=True).stdout.strip()
    path = os.path.join(WORK, "wt", sha)
    ok = subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    if os.path.isdir(path) and ok.stdout.strip() != sha:
        shutil.rmtree(path)  # half-created (a reboot in the middle of a checkout: empty files) — again
    if not os.path.isdir(path):
        subprocess.run(["git", "-C", REPO, "worktree", "prune"], capture_output=True)
        subprocess.run(["git", "-C", REPO, "worktree", "add", "--detach", "--force", path, sha], check=True, capture_output=True)
    return path, sha


def cut_transcript(src, marker, dst):
    """The conversation up to the line where the session received the message labelled marker (the run will feed it again)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    kept = 0
    with open(src, encoding="utf-8") as f, open(dst, "w", encoding="utf-8") as out:
        for line in f:
            if marker in line:
                try:
                    if json.loads(line).get("type") == "user":
                        break
                except ValueError:
                    pass
            out.write(line)
            kept += 1
    return kept


def synthetic(events, now):
    """A journal prefix right in the case (an invented history, no real journal): seq in order, time — just now,
    10 s apart between events — otherwise the questions to the owner in it would be stale by the run (ASK_TTL). An
    event with "ago" (seconds) happened that long before the run: for histories where time matters (deadlines)."""
    return [{"seq": i, "ts": round(now - e["ago"] if "ago" in e else now - (len(events) - i + 1) * 10, 3),
             "src": "eval", "type": e["type"],
             "entity": e.get("entity"), "data": e.get("data") or {}} for i, e in enumerate(events, 1)]


def copy_private(src, dst):
    """A copy of the login or settings — 0600, whatever the permissions of the source (mkstemp creates the file 0600)."""
    with open(src, encoding="utf-8") as f:
        text = f.read()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix="." + os.path.basename(dst) + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, dst)


def seed_files(case, run, work):
    """Files the case starts with: `files` — in the state folder (a relative name) or by an absolute path under $work;
    `config_files` — in the settings folder of the run (the driver points HUB_CONFIG_DIR at it): contacts.json and the like."""
    for key, base in (("files", os.path.join(run, "state")), ("config_files", os.path.join(run, "config"))):
        for name, text in (case.get(key) or {}).items():
            path = name if os.path.isabs(name) else os.path.join(base, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False))


def seed_repos(case):
    """`repos` — git repositories the case starts with: {absolute path under $work: {file: text or JSON}}, one commit
    each (a catalog of plugins the session connects with `hub marketplace add <path>`)."""
    for root, files in (case.get("repos") or {}).items():
        for name, text in files.items():
            path = os.path.join(root, *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False))
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "seed"]):
            subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.invalid",
                            "-c", "init.defaultBranch=main", *args], cwd=root, check=True, capture_output=True)


def prepare(case, wt, run, user_home=None, codex=None):
    """The folder of one run. user_home — where the claude login and the hub settings come from (by default the real ones): a parameter,
    so that a test never reads the real ones. codex — {"model", "effort"}: the run's core is a hub on Codex (no claude login
    is copied; the Codex sign-in goes in by codexrun.Auth)."""
    why = codexrun.claude_conversations(case) if codex is not None else None
    if why:
        raise SystemExit(why)
    given, user_home = user_home is not None, user_home or os.path.expanduser("~")
    os.makedirs(run, mode=0o700, exist_ok=True)
    os.chmod(run, 0o700)   # the run folder holds the claude login and keys
    os.makedirs(os.path.join(run, "state"), exist_ok=True)
    work = os.path.join(run, "work")
    # a Windows path with backslashes must be escaped by JSON
    case = json.loads(json.dumps(case, ensure_ascii=False).replace("$work", json.dumps(work)[1:-1])
                      .replace("$evals", json.dumps(HERE)[1:-1]))
    seed_files(case, run, work)
    seed_repos(case)
    if "events" in case:
        # the case's own history: nothing is read from the machine's hub — it runs anywhere and the same everywhere
        events = synthetic(case["events"], time.time())
        for e in [*events, *case.get("inject", [])]:
            cwd = (e.get("data") or {}).get("cwd")
            if isinstance(cwd, str) and cwd.startswith(work):
                os.makedirs(cwd, exist_ok=True)
        cfg = {}
    else:
        events = [e for e in journal.read(case.get("journal") or os.path.join(default_state_dir(), "journal.jsonl"))
                  if e["seq"] < case["cut_seq"]]
        # this hub's settings — by the same path as the core (HUB_CONFIG, the OS settings folder); a test supplies its own user_home
        cfg = read_json(os.path.join(user_home, ".config", "hub-core", "hub.json") if given
                        else catalog.config_path(default_state_dir()))
        # but not what reaches outside: the owner's MCP and connectors (live sign-ins), extensions and projects (the owner's
        # folders); sessions start in the journal's folders, a case that needs an MCP brings its own (config)
        for key in ("mcp", "connectors", "extensions", "projects"):
            cfg.pop(key, None)
        if codex is None:   # --host claude on a hub on Codex: the run's core is a claude hub (a case's own config may say otherwise)
            cfg.pop("host", None)
    with open(os.path.join(run, "state", "journal.jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
    home = os.path.join(run, "hub-home")
    os.makedirs(home, exist_ok=True)
    src = os.path.join(wt, "hub-home")
    for name in os.listdir(src):   # the hub folder of this code version: its CLAUDE.md and what it refers to
        if os.path.isfile(os.path.join(src, name)):
            shutil.copy(os.path.join(src, name), os.path.join(home, name))
    cfg.update(case.get("config") or {})   # a case with its own hub ({"hub": {"name": …}}) — the folder still comes from the run
    cfg["hub"] = {**cfg.get("hub", {}), "cwd": home}
    if codex is not None:   # a hub on Codex: every session of the run on <run>/state/codex-home
        own = cfg.get("codex") if isinstance(cfg.get("codex"), dict) else {}
        cfg.update(host="codex", codex={**own, **{k: v for k, v in codex.items() if v}})
    catalog.write_private(os.path.join(run, "hub.json"), cfg)
    ccfg = os.path.join(run, "claude-config")
    os.makedirs(ccfg, exist_ok=True)
    for src_file, name in ((os.path.join(user_home, ".claude", ".credentials.json"), ".credentials.json"),
                           (os.path.join(user_home, ".claude.json"), ".claude.json")):
        if os.path.exists(src_file) and codex is None:
            copy_private(src_file, os.path.join(ccfg, name))
    cwds, threads = {"hub": home, "hub-core": home}, {}
    for name, t in (case.get("transcripts") or {}).items():
        if codex is not None:
            threads[name], kept = codexrun.cut_rollout(t["path"], t["marker"], codexrun.run_home(run))
        else:
            cwd = cwds.get(name) or t["cwd"]
            kept = cut_transcript(t["path"], t["marker"], os.path.join(ccfg, "projects", slug(cwd), os.path.basename(t["path"])))
        if not kept:
            raise SystemExit(f"{case['id']}: conversation {name} is empty before the marker {t['marker']}")
    resolved = {**case, "cwd": cwds, **({"host": "codex", "codex_threads": threads} if codex is not None else {})}
    path = os.path.join(run, "case.json")
    write_json(path, resolved)
    return path


# --- checks ----------------------------------------------------------------------------------------------------

def _get(e, key):
    cur = e
    for part in key.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _match(e, match, ctx):
    for key, want in match.items():
        got = _get(e, key)
        if want == "$present":
            if got in (None, ""):
                return False
        elif want == "$absent":
            if got not in (None, ""):
                return False
        elif isinstance(want, dict) and "re" in want:
            if not re.search(want["re"], json.dumps(got, ensure_ascii=False) if not isinstance(got, str) else got, re.I | re.S):
                return False
        elif isinstance(want, list):
            if got not in [ctx.get(w, w) for w in want]:
                return False
        elif got != ctx.get(want, want):
            return False
    return True


def check(c, events, ctx):
    after = [e for e in events if e["seq"] >= ctx["$inject_seq"]]
    if c["type"] in ("event", "no_event"):
        n = sum(_match(e, c["match"], ctx) for e in after)
        ok = n >= c.get("min", 1) if c["type"] == "event" else n == 0
        return ok, f"found {n}"
    if c["type"] in ("said", "not_said"):
        texts = [e["data"].get("text", "") for e in after if e["type"] == "session.said" and e["entity"] == ctx["$target"]]
        hit = [t for t in texts if re.search(c["re"], t, re.I | re.S)]
        ok = bool(hit) if c["type"] == "said" else not hit
        return ok, (hit[0][:160] if hit else f"{len(texts)} replies, no match")
    if c["type"] == "said_before_reply":
        # session replies after the feed but before the turn started by the fed message (turns for core messages from the queue):
        # each must match re ("—" or a single line)
        mark = ctx["$inject"]
        start = next((e["seq"] for e in after if e["type"] == "turn.started" and e["entity"] == ctx["$target"]
                      and mark in ((e.get("data") or {}).get("marker") or "")), None)
        texts = [e["data"].get("text", "") for e in after if e["type"] == "session.said" and e["entity"] == ctx["$target"]
                 and (start is None or e["seq"] < start)]
        bad = [t for t in texts if not re.search(c["re"], t, re.I | re.S)]
        return not bad, (f"{len(texts)} replies before the answer, {len(bad)} do not match: " + bad[0][:120]) if bad else f"{len(texts)} replies before the answer, all match"
    if c["type"] == "said_total":
        # everything the session said after the feed (except "—"): how many messages, how many characters, and re over all of it together
        texts = [e["data"].get("text", "") for e in after if e["type"] == "session.said" and e["entity"] == ctx["$target"]
                 and e["data"].get("text", "").strip() not in ("—", "-")]
        total = sum(len(t) for t in texts)
        ok = len(texts) <= c.get("max_msgs", 99) and total <= c.get("max_chars", 10 ** 9) and bool(re.search(c["re"], "\n".join(texts), re.I | re.S))
        return ok, f"{len(texts)} msgs, {total} chars"
    if c["type"] == "mcp_calls":
        # calls of the fake MCP (its own log in the run folder): how many times the tool was called
        log = mcp_log(ctx)
        if not any(x["tool"] == "_init" for x in log):
            return False, "the fake MCP did not come up (no _init record): \"no calls\" proves nothing"
        n = sum(1 for x in log if x["tool"] == c["tool"])
        return c.get("min", 0) <= n <= c.get("max", 10 ** 6), f"calls of {c['tool']}: {n}"
    if c["type"] == "mcp_read_first":
        # the chat was read before the owner was shown the text (the card with the draft, or a plain reply), and before any send
        read = [x["t"] for x in mcp_log(ctx) if x["tool"] == c["tool"]]
        shown = [e["ts"] for e in after if e["type"] == "permission.requested" and e["entity"] == ctx["$target"]]
        # no card: the first plain-text draft, not the last reply (draft, then reading, then "waiting for yes" must be red)
        shown = shown or [e["ts"] for e in after if e["type"] == "session.said" and e["entity"] == ctx["$target"]][:1]
        sent = [x["t"] for x in mcp_log(ctx) if x["tool"] not in (c["tool"], "_init")]
        if not read:
            return False, "the chat was not read"
        if not shown and not sent:
            return False, "no text shown"
        return read[0] < min(shown + sent), f"read {read[0]:.1f}, text/send {min(shown + sent):.1f}"
    if c["type"] in ("file", "no_file"):
        # a file in the state folder at the end of the run: lines_min/lines_max — its length, re/not_re — its text
        path = os.path.join(ctx["$run"], "state", c["path"])
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read()
        except OSError:
            body = None
        if c["type"] == "no_file":
            return body is None, "no file" if body is None else f"the file is there, {len(body.splitlines())} lines"
        if body is None:
            return False, "no file"
        lines = [x for x in body.splitlines() if x.strip()]
        ok = c.get("lines_min", 0) <= len(lines) <= c.get("lines_max", 10 ** 6)
        for key, want in (("re", True), ("not_re", False)):
            if key in c:
                ok = ok and bool(re.search(c[key], body, re.I | re.S)) == want
        return ok, f"{len(lines)} lines: " + " | ".join(lines)[:140]
    if c["type"] == "any_of":
        got = [check(x, events, ctx) for x in c["checks"]]
        return any(ok for ok, _ in got), " | ".join(note for _, note in got)
    raise SystemExit(f"unknown check {c['type']}")


def mcp_log(ctx):
    try:
        with open(os.path.join(ctx["$run"], "work", "chat.log"), encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def judge(case, run):
    with open(os.path.join(run, "events.jsonl"), encoding="utf-8") as f:
        events = [json.loads(line) for line in f]
    drv = read_json(os.path.join(run, "driver.json"))
    # several fed messages (a /compact, then the message under test): the turn under judgement is the last one's, as
    # the driver waits for it
    inj = ([e for e in events if (e.get("data") or {}).get("eval_inject")] or [None])[-1]
    if not inj or not drv["finished"]:
        return None, [("n/a", False, "the session did not finish its turn" if inj else "the message was not fed")], events
    target = case["real"][0]
    if any(e["type"] == "session.said" and e["entity"] == target and e["seq"] >= inj["seq"]
           and LOGGED_OUT.search(e["data"].get("text", "")) for e in events):
        return None, [("n/a", False, "claude is not logged in (CLAUDE_CODE_OAUTH_TOKEN or the ~/.claude login)")], events
    resolved = os.path.join(run, "case.json")
    on = os.path.exists(resolved) and read_json(resolved).get("host") == "codex"
    why = codexrun.lost(events, inj["seq"], target) if on else None
    if drv.get("cut_unused"):   # the core started the session fresh: the cut thread was not continued
        why = f"the cut thread was not continued ({', '.join(drv['cut_unused'])}): the session has no turn before cut_seq — the core started it fresh"
    if why:
        return None, [("n/a", False, why)], events
    ctx = {"$inject": f"m{inj['seq']}", "$inject_seq": inj["seq"], "$target": target, "$run": run}
    results = [(c["id"], *check(c, events, ctx)) for c in case["checks"]]
    # a check with "observe" is a measurement, not a requirement: it does not fail the run, the note says what was seen
    results = [(cid, OBS, note) if c.get("observe") else (cid, ok, note) for c, (cid, ok, note) in zip(case["checks"], results)]
    return all(ok for _, ok, _ in results), results, events


# the status of an observation in a result line: not a pass and not a fail, the note carries what was seen
OBS = "obs"


def mark(status):
    return "obs" if status == OBS else "ok" if status else "FAIL"


def run_one(case, wt, run, codex=None, auth=None, user_home=None):
    """One run: the folder, the driver, the verdict. On Codex the run gets a copy of the evals' sign-in (auth) and gives the
    refreshed one back whatever happens; the run's Codex folder (copies of the conversation, the token) is not kept."""
    try:
        resolved = prepare(case, wt, run, user_home=user_home, codex=codex)
        if auth is not None:
            auth.give(run)
        limit = case.get("timeout", 900) + 120
        with open(os.path.join(run, "driver.log"), "w") as log:
            # its own process group: the sessions' processes (codex app-server) end with it, before the sign-in goes back —
            # a leftover one could refresh the one-time token after take()
            p = subprocess.Popen([sys.executable, os.path.join(HERE, "driver.py"), wt, run, resolved], stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
            try:   # whatever happens (a timeout, Ctrl-C) — the group ends before the sign-in goes back
                done = exited(p, limit)
            finally:
                end_group(p)
            if not done:
                return None, [("n/a", False, f"the run did not fit into {limit} s")], []
    finally:
        if auth is not None:
            auth.take(run)
        if codex is not None:
            shutil.rmtree(codexrun.run_home(run), ignore_errors=True)
    return judge(case, run)


def exited(p, limit):
    """Wait up to limit s for the driver to exit WITHOUT reaping it: an unreaped leader keeps its pid, so the group id
    end_group kills cannot be another process's by then. Windows, and macOS before Python 3.13 (no os.waitid) — a plain
    wait."""
    if os.name == "nt" or not hasattr(os, "waitid"):
        try:
            p.wait(timeout=limit)
            return True
        except subprocess.TimeoutExpired:
            return False
    end = time.time() + limit
    while time.time() < end:
        if os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG):
            return True
        time.sleep(0.2)
    return False


def end_group(p):
    """The driver and whatever it left in its process group (the sessions' processes), then the driver is reaped. On
    Windows only the driver: there evals on Codex are not run (no process group). Without os.waitid the leader is
    already reaped — its group is still killed: what the sessions left in it ends too."""
    if os.name != "nt" and (p.returncode is None or not hasattr(os, "waitid")):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    elif p.poll() is None:
        p.kill()
    p.wait()


def codex_of(a):
    """--host codex (or a hub on Codex): the sessions' model and effort — the arguments, else the hub's hub.json codex."""
    from core import codexmodel
    model, effort = codexmodel.picks(cfg=None if on_codex() else {})["session"]
    model, effort = a.codex_model or model, a.codex_effort or effort   # no effort — Codex's own, as the hub's sessions
    if not model:
        raise SystemExit("--codex-model <the model of Codex sessions>: hub.json has no codex.model")
    return {"model": model, "effort": effort}


def main():
    guard_codex()   # no codex of the run without a CODEX_HOME of its own (tools/codex_guard.py)
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--before")
    ap.add_argument("--after", default="HEAD")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--only", choices=("before", "after"))
    ap.add_argument("--host", choices=("claude", "codex"), help="by default — the hub's (hub.json host)")
    ap.add_argument("--codex-model")
    ap.add_argument("--codex-effort")
    ap.add_argument("--codex-login", action="store_true", help="the Codex sign-in of the evals — a code on the phone")
    ap.add_argument("--codex-status", action="store_true")
    ap.add_argument("--codex-logout", action="store_true")
    a = ap.parse_args()
    state_dir = default_state_dir()
    for flag, do in ((a.codex_login, codexrun.login), (a.codex_status, codexrun.status), (a.codex_logout, codexrun.logout)):
        if flag:
            sys.exit(do(state_dir))
    if not a.cases:
        ap.error("at least one case is needed")
    a.cases = [find_case(c) or ap.error(f"no such case: {c}") for c in a.cases]   # a path, or a name in any case folder
    codex = codex_of(a) if (a.host or ("codex" if on_codex() else "claude")) == "codex" else None
    for path in a.cases if codex is not None else ():   # before any run spends the quota
        why = codexrun.claude_conversations(read_json(path))
        if why:
            raise SystemExit(why)
    with (codexrun.Auth(state_dir) if codex is not None else contextlib.nullcontext()) as auth:
        run_all(a, codex, auth)


def run_all(a, codex, auth):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    lines = [f"# Evals {stamp}", "", "| Case | Variant | Passed | Failures |", "|---|---|---|---|"]
    spent, tokens = 0.0, [0, 0]
    for path in a.cases:
        case = read_json(path)
        variants = {"before": a.before or case.get("before"), "after": a.after}
        for vname, rev in variants.items():
            if not rev or (a.only and a.only != vname):
                continue
            wt, sha = worktree(rev)
            passed, na, fails = 0, 0, []
            for n in range(1, a.trials + 1):
                run = os.path.join(WORK, "runs", f"{stamp}-{case['id']}", f"{vname}-{sha}-{n}")
                ok, results, events = run_one(case, wt, run, codex=codex, auth=auth)
                said = [e["data"].get("text", "") for e in events if e["type"] == "session.said"
                        and e["entity"] == case["real"][0]]
                # cost of the run: claude's cost_usd accumulates per process — the last result of the session under test (one
                # real session without a restart; otherwise — a lower bound)
                results_of = [e["data"] for e in events if e["type"] == "turn.result" and e["entity"] == case["real"][0]]
                if codex is not None:   # Codex: no price — tokens of the plan, per turn
                    tokens = [tokens[0] + sum(d.get("tokens_in") or 0 for d in results_of),
                              tokens[1] + sum(d.get("tokens_out") or 0 for d in results_of)]
                else:
                    spent += (results_of[-1].get("cost_usd") or 0) if results_of else 0
                with open(os.path.join(run, "trace.md"), "w") as f:
                    f.write(f"# {case['id']} · {vname} ({sha}) · trial {n}\n\n")
                    for cid, cok, note in results:
                        f.write(f"- {'👁' if cok == OBS else '✅' if cok else '❌'} {cid}: {note}\n")
                    f.write("\n## What the session said\n\n" + "\n\n---\n\n".join(said) + "\n")
                    f.write("\n## Facts after the cut\n\n" + "\n".join(
                        f"{e['seq']} {e['type']} {e.get('entity') or ''} {json.dumps(e.get('data'), ensure_ascii=False)[:200]}"
                        for e in events if e["type"] not in ("turn.tool_result", "session.info", "host.done")) + "\n")
                if ok is None:
                    na += 1
                elif ok:
                    passed += 1
                else:
                    fails += [cid for cid, cok, _ in results if not cok]
                print(f"{case['id']} {vname}#{n}: {'n/a' if ok is None else 'ok' if ok else 'FAIL'} "
                      + "; ".join(f"{cid}={mark(cok)} ({note[:80]})" for cid, cok, note in results), flush=True)
            lines.append(f"| {case['id']} | {vname} `{sha}` | {passed}/{a.trials - na}" + (f" (+{na} n/a)" if na else "")
                         + f" | {', '.join(sorted(set(fails))) or '—'} |")
    report = os.path.join(WORK, "runs", f"{stamp}-report.md")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nCodex tokens: input {tokens[0]}, output {tokens[1]}" if codex is not None else f"\nprice: ${spent:.2f}")
    print(f"report: {report}")


if __name__ == "__main__":
    main()
