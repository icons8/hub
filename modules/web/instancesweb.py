"""Settings -> Main -> Hubs on this computer (js/instances.js; the spec of several hubs, 2.5, 2.6, 2.10, 2.12).

GET /api/instances — the hubs of this machine (core/instancecli.rows: the main one and the registry), each with what
the page shows: name and title, command, page port, running or not, engine, its own Claude sign-in or the shared one,
which of them is this hub; nothing of another hub's data (sessions, journal). Plus whether a hub can be added here and
why not, the engines to choose from (Claude Code and Codex — a hub on Codex without the program on the machine gets
its own copy), whether the machine has a codex, and the last job of this page with the last lines of its log.

POST /api/instances — the owner's step from the page (POST with X-Hub from this page, like "Return to the clone"):
{op: "new", name, engine?, claude?} — `hub instance new … --dry-run` first, in a process of its own (the same refusals
as in the terminal, at once), then run apart (core/instancejob.py): the install neither holds this server nor
dies with its restart; {op: "remove", name, confirm | password} — `hub instance uninstall <name>` of an instance (never
the main hub: `hub uninstall`), confirm must repeat the name — or, for a hub with a password, be its password; the data
stays; {op: "clear"} — forget a finished job; {op: "password", name, new, current?} — set or change the page's password
of a hub of this machine (core/pagepass.py: its settings folder), the current one whenever it has one (the page of one
hub proves nothing about another); {op: "password_off", name, current} — take it off. A hub whose running code knows no
password (its /api/about has no `signin`) is refused: the file would lie next to a server that keeps the page open."""
import functools
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time

from core import i18n, instance, instancecli, instancejob, pagepass, uninstall

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNNER = os.path.join(REPO, "core", "instancejob.py")
HUB = os.path.join(REPO, "bin", "hub")
# s: a job without the runner's pid is "running" this long (systemd-run, WMI on a cold Windows start it a minute later at
# worst); then its runner is asked by its pid — gone without an end (killed, the machine restarted, never started) is
# "lost". JOB_LIMIT: past it a job is "lost" whatever its pid says (a number reused by a long-lived process)
START_GRACE, JOB_LIMIT = 120, 2 * 3600
_lock = threading.Lock()   # one job at a time: the check of the last job, the dry run and the write under it
LOG_LINES = 8
DROP_ENV = ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE", *instancecli.OWN_ENV)


def _settings(config_dir):
    return instance._settings(os.path.join(config_dir, "hub.json")) if config_dir else {}


def _same(a, b):
    return bool(a and b) and os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def refusal():
    """Why a hub cannot be added from here, or None: no main hub stands (a stand, a copy), or the code cannot be cloned.
    -> (i18n key, params): each tab is told in its own language."""
    if not uninstall.marked(instance.main_state()):
        return "cli.instance.no_main", {}
    try:
        instancecli.code_source(instancecli.main_code())
    except instancecli.Refused as e:
        return (e.key, e.params) if e.key else ("web.instances.cannot_add", {"why": str(e)})
    return None


@functools.cache
def built():
    """OpenAI builds Codex CLI for this machine: asked once a process (an Intel Mac's answer is a sysctl)."""
    try:
        from core import codexinstall
        return codexinstall.target() is not None
    except Exception:  # noqa: BLE001 — no Codex install in this code: no choice, not a failure
        return False


def engines(found=None):
    """What the form offers: Claude Code and Codex, at every hub (decided 9.5: a new hub on Codex without the program
    gets its own copy — core/codexinstall.for_instance); Codex where OpenAI builds it for this machine or the machine
    already has one a new hub takes (found — codex_found())."""
    ok = built() or (codex_found() if found is None else found)
    return ["claude", "codex"] if ok else ["claude"]


def codex_found():
    """The machine has a codex a new hub would take (core/codexinstall.taken): the form says otherwise that it
    downloads one."""
    try:
        from core import codexinstall
        return bool(codexinstall.taken())
    except Exception:  # noqa: BLE001 — no answer is "not found": the form only says more
        return False


def hubs(state_dir, mine=None, machine=None):
    """mine — this hub's own settings as its page reads them (HUB_CONFIG, a stand's folder): its row by them; machine —
    core/instancecli.rows already read."""
    out = []
    for row in instancecli.rows() if machine is None else machine:
        me = _same(row.get("state"), state_dir)
        cfg = mine if me and mine is not None else _settings(row.get("config_dir"))
        name = row.get("name") or ""
        signin = pagepass.state(None if me else row.get("config_dir") or None) if me or row.get("config_dir") else "open"
        out.append({"name": name, "main": not name, "me": me, "signin": signin,
                    "password_ok": True if me else knows_password(row),
                    "title": str(cfg.get("title") or row.get("title") or name or "")[:100],
                    "command": row.get("command") or "", "port": (row.get("ports") or {}).get("web"),
                    "running": bool(row.get("running")), "gone": bool(row.get("gone")),
                    "port_busy": bool(row.get("port_busy")), "engine": row.get("engine") or "claude",
                    "claude": "own" if cfg.get("claude_config_dir") else "shared"})
    return out


_knows = {}   # (port, state) -> (monotonic time, answer): a neighbour is asked once a KNOWS_TTL, not on every poll
KNOWS_TTL = 60


def knows_password(row):
    """The hub's code takes a password (core/pagepass.py): a running one says so in /api/about (`signin`, short or
    whole), a stopped one has the file in its code; no answer — no."""
    port = (row.get("ports") or {}).get("web")
    if row.get("running") and port:
        at, got = _knows.get((port, row.get("state")), (None, False))
        if at is None or time.monotonic() - at > KNOWS_TTL:
            said = _ask_signin(port)
            if said is not None:   # no answer (a restart, a busy moment): what it said before, asked again next time
                got = said
                _knows[(port, row.get("state"))] = (time.monotonic(), got)
        return got
    code = (row.get("code") or {}).get("path") if isinstance(row.get("code"), dict) else None
    if not code and row.get("name"):
        try:
            code = instancecli.main_code()
        except Exception:  # noqa: BLE001 — not known: not offered
            return False
    return bool(code) and os.path.isfile(os.path.join(code, "core", "pagepass.py"))


def _ask_signin(port):
    """The neighbour's /api/about names `signin` (its code knows the password; the short card answers without signing
    in): True | False, None — no answer."""
    import json
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1, never the machine's proxy
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{int(port)}/api/about?lite=1",
                                     headers={"Host": f"127.0.0.1:{int(port)}"})
        with opener.open(req, timeout=2) as r:
            return isinstance(json.loads(r.read(1 << 20) or b"{}").get("signin"), str)
    except (OSError, ValueError, AttributeError):
        return None


def _tail(state_dir):
    try:
        with open(os.path.join(state_dir, instancejob.LOG), encoding="utf-8", errors="replace") as f:
            lines = [x.rstrip() for x in f.readlines()[-200:] if x.strip()]
    except OSError:
        return []
    return [x[:300] for x in lines[-LOG_LINES:]]


_born = {}   # (pid, born) -> (monotonic time, same process): the start time is a `ps` on macOS — not on every poll


def _runner_alive(got):
    import platforms
    pid, born = got.get("pid"), got.get("born")
    if not isinstance(pid, int) or not platforms.current.alive(pid):
        return False
    if born is None:
        return True
    at, same = _born.get((pid, born), (None, None))
    if at is None or time.monotonic() - at > 10:
        now = platforms.current.process_start(pid)
        same = now is None or now == born   # not read this time: not a verdict that it is another process
        _born.clear()
        _born[(pid, born)] = (time.monotonic(), same)
    return same


def job(state_dir, now=None):
    """The page's last job: {op, name, state: running|done|failed|lost, lines}, or None."""
    got = instancejob.read(state_dir)
    if not got or got.get("op") not in instancejob.OPS or not isinstance(got.get("name"), str):
        return None
    now = time.time() if now is None else now
    if "ended" in got:
        state = "done" if got.get("code") == 0 else "failed"
    elif "pid" not in got:
        state = "running" if now - (got.get("started") or 0) < START_GRACE else "lost"
    elif now - (got.get("started") or 0) > JOB_LIMIT:
        state = "lost"
    else:
        state = "running" if _runner_alive(got) else "lost"
    out = {"op": got["op"], "name": got["name"], "state": state, "started": got.get("started")}
    if state != "running":
        out["lines"] = _tail(state_dir)
    return out


_seen = {}   # state folder -> (monotonic time, job mark, the machine's part): the services asked once a while
FRESH = 3


def status(web):
    """The machine's part is asked again after FRESH s and when the job's state changes (the list then has the new hub,
    or not the removed one, in the same answer that says "done")."""
    st = web.state_dir
    now = job(st)
    mark = (now or {}).get("state"), (now or {}).get("started")
    at, seen, part = _seen.get(st, (None, None, None))
    if at is None or time.monotonic() - at > FRESH or mark != seen:
        found = codex_found()
        machine = instancecli.rows()
        rows = hubs(st, web.config() or {}, machine)
        said = {h["name"]: h["password_ok"] for h in rows}   # asked once per neighbour (its /api/about)
        part = {"hubs": rows, "add": refusal(), "engines": engines(found), "codex_found": found,
                # the hubs of this account without a password (core/pagepass.unprotected): they open the others
                "unprotected": pagepass.unprotected(lambda row: said.get(row.get("name") or "", False), st, machine)}
        _seen[st] = (time.monotonic(), mark, part)
    no = part["add"]
    return {**part, "add": web.t(no[0], **no[1]) if no else None, "job": now, "engine": "codex" if (web.config() or {}).get("host") == "codex" else "claude"}


def command(state_dir, job_id, env, which=shutil.which, osname=None):
    """argv of the runner: on Linux under the hub's systemd unit a unit of its own (stopping the service takes its whole
    cgroup — and removing this hub stops it), elsewhere the process itself (started apart by spawn())."""
    cmd = [sys.executable, RUNNER, state_dir, job_id]
    import platforms
    if (osname or platforms.current.APP_OS) == "linux" and env.get("INVOCATION_ID") and which("systemd-run"):
        keep = ["PATH", "XDG_CONFIG_HOME", "HUB_LANG", "LANG"]
        return ["systemd-run", "--user", "--collect", "--quiet", "--unit", f"hub-instance-job-{job_id}",
                *[f"--setenv={k}={env[k]}" for k in keep if env.get(k)], "--", *cmd]
    return cmd


def spawn(cmd, env):
    """Start the runner apart from this server: Windows — outside hub-up's job (WMI); elsewhere its own session."""
    import platforms
    if platforms.current.APP_OS == "windows" and platforms.current.popen_outside(cmd):
        return
    platforms.current.popen_detached(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)


def check(argv, lang):
    """`hub instance new … --dry-run` (it writes nothing): every refusal of the terminal, at once, in the owner's
    language — its own process, as the language of this server's process is not the request's. -> the reason or None."""
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    try:
        r = subprocess.run([sys.executable, HUB, "instance", "new", *argv], env={**env, "HUB_LANG": lang or "en"},
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                           stdin=subprocess.DEVNULL, **_apart())
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if r.returncode == 0:
        return None
    return (r.stderr or r.stdout).strip()[-500:] or f"hub instance new: {r.returncode}"


def _apart():
    import platforms
    return platforms.current.apart()


def _start(web, data):
    st = web.state_dir
    job_id = secrets.token_hex(6)
    data = {**data, "id": job_id, "started": int(time.time()), "lang": web.lang_now()}
    instancejob.write(st, data)
    _seen.pop(st, None)
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    try:
        spawn(command(st, job_id, env), env)
    except Exception as e:  # noqa: BLE001 — not started, whatever the reason: no job left that would look "running"
        try:
            os.unlink(instancejob.path(st))
        except OSError:
            pass
        return 500, web.t("web.instances.not_started", why=str(e))
    return 202, {"ok": True}


def act(web, body, h=None):
    """-> (code, answer or the reason in the request's language[, the Set-Cookie of this tab]). Under one lock: two tabs
    pressing at once do not both pass the "no job running" check and start two installs."""
    if body.get("op") == "clear":   # not behind another tab's dry run: forgetting a finished job needs no lock
        return _act(web, body)
    if body.get("op") in ("password", "password_off"):   # no install involved: not behind another tab's job
        return password(web, body, h)
    with _lock:
        return _act(web, body)


def _row(web, name):
    """The hub's row and whether it is this one; this hub always has its own (a copy or a stand outside the registry)."""
    for row in instancecli.rows():
        if (row.get("name") or "") == name:
            return row, _same(row.get("state"), web.state_dir)
    if name == (instance.name(web.state_dir) or ""):
        return {"name": name, "command": instance.command_name(name or None), "state": web.state_dir}, True
    return None, False


def _check_current(web, body, folder):
    """The hub's current password, against its own counter of tries (core/pagepass.attempt). -> None or the refusal."""
    verdict, wait = pagepass.attempt(body.get("current") if isinstance(body.get("current"), str) else "", folder)
    if verdict == "ok":
        return None
    key, params = pagepass.said(verdict, wait)
    return 429 if verdict == "locked" else 403, web.t(key, **params)


def password(web, body, h=None):
    """Set, change or take off the page's password of a hub of this machine (core/pagepass.py)."""
    name = body.get("name")
    if not isinstance(name, str) or len(name) > 40:
        return 400, web.t("web.instances.bad_name")
    row, me = _row(web, name)
    if row is None:
        return 404, web.t("web.instances.unknown", name=name)
    folder = None if me else row.get("config_dir")
    if not folder and not me:
        return 409, web.t("web.pass.no_folder")
    if not me and not knows_password(row):
        return 409, web.t("web.pass.old_code")
    st = pagepass.state(folder)
    if st == "key" and not me:   # its key holds the door now, and this page does not know the key
        return 409, web.t("web.pass.key_elsewhere")
    if st == "password":
        refused = _check_current(web, body, folder)
        if refused:
            return refused
    elif st == "broken":
        return 409, web.t("web.pass.broken", cmd=row.get("command") or "hub")
    _seen.pop(web.state_dir, None)
    if body["op"] == "password_off":
        pagepass.take_off(folder)
        return 200, {"ok": True, "signin": "open"}
    new = body.get("new")
    why = pagepass.fits(new)
    if why:
        return 400, web.t(why, min=pagepass.MIN_LEN, max=pagepass.MAX_LEN)
    rec = pagepass.set_password(new, folder)
    if not me or h is None:
        return 200, {"ok": True, "signin": "password"}
    from modules.web import signin
    return 200, {"ok": True, "signin": "password"}, pagepass.set_cookie(rec, secure=signin.secure(web, h))


def _act(web, body):
    op = body.get("op")
    st = web.state_dir
    if op == "clear":
        got = job(st)
        if got and got["state"] == "running":
            return 409, web.t("web.instances.busy")
        try:
            os.unlink(instancejob.path(st))
        except OSError:
            pass
        return 200, {"ok": True}
    if op not in ("new", "remove"):
        return 400, web.t("web.instances.bad_op")
    name = body.get("name")
    if not isinstance(name, str) or not name or len(name) > 40:
        return 400, web.t("web.instances.bad_name")
    got = job(st)
    if got and got["state"] == "running":
        return 409, web.t("web.instances.busy")
    if op == "new":
        no = refusal()
        if no:
            return 409, web.t(no[0], **no[1])
        engine, claude = body.get("engine") or "claude", body.get("claude") or "shared"
        if engine not in engines() or claude not in ("shared", "own"):
            return 400, web.t("web.instances.bad_choice")
        argv = [name, *(["--engine", "codex"] if engine == "codex" else []),
                *(["--claude", "own"] if engine == "claude" and claude == "own" else []), "--dry-run"]
        why = check(argv, web.lang_now())
        if why:
            return 409, why
        return _start(web, {"op": "new", "name": name, "engine": engine,
                            "claude": claude if engine == "claude" else None})
    if not any(e["name"] == name for e in instance.entries()):
        return 404, web.t("web.instances.unknown", name=name)
    row, _ = _row(web, name)
    folder = (row or {}).get("config_dir")
    if folder and pagepass.state(folder) == "password":   # a hub with a password: its password, not its name
        refused = _check_current(web, {"current": body.get("password")}, folder)
        if refused:
            return refused
    elif body.get("confirm") != name:
        return 400, web.t("web.instances.confirm_name")
    return _start(web, {"op": "remove", "name": name})
