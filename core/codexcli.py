"""Codex host (experimental second engine): `hub codex login|status|logout` — the hub's
Codex sign-in into its CODEX_HOME (<state folder>/codex-home), not into the owner's personal ~/.codex: the hub has its
own token chain (a copy of someone else's auth.json breaks when one of the copies refreshes the one-time refresh token).

The sign-in is by a device code through `codex app-server` (account/login/start type chatgptDeviceCode): the protocol
gives the link and the code as fields, no output to parse; the end is the notification account/login/completed. Signed
in — the fact `codex.login` (email and plan — for "Settings -> Codex"), signed out — `codex.logout`. The page signs in the
same way with a background process (`python3 core/codexcli.py wait <state folder>`): the link and the code go to
<folder>/codex-login/info.json, the result to result.json.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import codexslot, i18n, spool  # noqa: E402
from core.modelrule import MODEL_ID  # noqa: E402 — goes into codex/<id> and thread/start

LOGIN_WAIT = 900   # a device code lives ~15 min


def home_of(state_dir):
    return os.path.join(state_dir, "codex-home")


def args(state_dir, home):
    """What every `codex app-server` in a Codex folder gets after its subcommand (`-c` before it is lost): an account
    folder of the Codex pool (core/codexslot.py) keeps its indexes in the hub folder — the threads' store is one, and a
    run without it (a sign-in, a module's measurement) would start its own databases there (probe 2026-11-05)."""
    hub = os.path.abspath(home_of(state_dir))
    pool = os.path.abspath(os.path.join(state_dir, "moddata")) + os.sep
    if not home or not os.path.abspath(home).startswith(pool):
        return []   # the hub folder, or a folder of its own (the evals' sign-in): its own store
    return ["-c", f"sqlite_home={json.dumps(hub)}"]


ACTIVE = "codex-active.json"   # hubd: the Codex pool's active account and its folder — for processes without the state


def active_home(state_dir):
    """The Codex folder of the pool's active account for a process that has no state (the core's model, the critic):
    <state>/codex-active.json (hubd writes it on a change); none, broken or a folder not fit now — the hub folder."""
    try:
        with open(os.path.join(state_dir, ACTIVE), encoding="utf-8") as f:
            home = json.load(f).get("home")
    except (OSError, ValueError, AttributeError):
        return home_of(state_dir)
    if isinstance(home, str) and os.path.isabs(home) and not codexslot.fit(state_dir, home):
        return home
    return home_of(state_dir)


def _prepare(state_dir, home=None):
    """The hub's Codex folder (or the given one — the evals' sign-in) with the core's settings (the sign-in as a file in
    it) — also before hubd's first start."""
    from hosts.codex import CONFIG
    home = home or home_of(state_dir)
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "config.toml")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(CONFIG)
    return home


class Rpc:
    """A short conversation with `codex app-server` in the hub's Codex folder (or the given home): requests and
    notifications, no model."""

    def __init__(self, state_dir, home=None):
        from hosts import codex_set
        from hosts.codex import codex_bin, codex_env
        home = _prepare(state_dir, home)
        env = {**codex_env(os.environ), "CODEX_HOME": home}
        env.pop("HUB_SESSION", None)
        self.p = subprocess.Popen([codex_bin(), "app-server", *args(state_dir, home), *codex_set.args()],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, cwd=home)
        self.next, self.notes, self.got = 1, [], {}
        # lines go to a queue from a thread: select on the pipe does not see lines already read into readline's buffer
        # (two lines in one chunk — the second would wait until the timeout)
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "agents-hub", "version": "codexcli"}})
            self.p.stdin.write(json.dumps({"method": "initialized"}) + "\n")
            self.p.stdin.flush()
        except (OSError, RuntimeError):
            self.close()
            raise

    def _pump(self):
        for line in self.p.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _read(self, timeout):
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            self.lines.put(None)
            raise RuntimeError(i18n.t("cli.codex.rpc_closed"))
        try:
            msg = json.loads(line)
        except ValueError:
            return {}
        if "id" in msg and "method" not in msg:
            self.got[msg["id"]] = msg
        elif "method" in msg:
            self.notes.append(msg)
        return msg

    def call(self, method, params, timeout=30):
        rid = self.next
        self.next += 1
        self.p.stdin.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        end = time.time() + timeout
        while rid not in self.got:
            if time.time() > end:
                raise RuntimeError(i18n.t("cli.codex.rpc_silent", method=method))
            self._read(max(0.0, end - time.time()))
        msg = self.got.pop(rid)
        if "error" in msg:
            raise RuntimeError((msg["error"] or {}).get("message") or i18n.t("cli.codex.rpc_refused", method=method))
        return msg.get("result") or {}

    def wait_note(self, method, timeout):
        end = time.time() + timeout
        while True:
            for i, n in enumerate(self.notes):
                if n.get("method") == method:
                    return self.notes.pop(i).get("params") or {}
            if time.time() > end:
                return None
            self._read(min(1.0, max(0.0, end - time.time())))

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.p.kill()


DEAD_TOKEN = re.compile(r"\b401\b|unauthorized|revoked|could not be refreshed|sign in again", re.I)
UNREACHABLE = re.compile(r"error sending request|dns|connect|timed out|timeout|network|unreachable|tls|certificate", re.I)


def dead_token(error):
    """The app-server says the sign-in file is dead (a revoked or expired refresh token, a refused token: 401) — not that
    the network or the server is out of reach: a dead one is signed out by the core's own hand, an unreachable one
    leaves the file alone (the token may still be good)."""
    text = str(error)
    return bool(DEAD_TOKEN.search(text)) and not UNREACHABLE.search(text)


def drop_sign_in(state_dir, home=None):
    """Delete the sign-in file of that Codex folder (only it: the folder's config, threads and indexes stay)."""
    try:
        os.remove(os.path.join(home or home_of(state_dir), "auth.json"))
    except FileNotFoundError:
        pass


def open_rpc(state_dir, home=None):
    """Rpc, and a sign-in file the app-server refuses on its start (a dead token) is dropped and the start repeated."""
    try:
        return Rpc(state_dir, home)
    except RuntimeError as e:
        if not dead_token(e):
            raise
    drop_sign_in(state_dir, home)
    return Rpc(state_dir, home)


def account(rpc):
    """(email, plan) of the sign-in or None."""
    acc = rpc.call("account/read", {}).get("account")
    if not acc:
        return None
    return acc.get("email") or acc.get("type"), acc.get("planType")


def windows_line(res):
    from hosts import codex_limits
    snap = (res.get("rateLimitsByLimitId") or {}).get("codex") or res.get("rateLimits") or {}
    fact = codex_limits.limits_fact(snap, allowed=res.get("ordinaryUsageAllowed"))
    names = {"five_hour": i18n.t("cli.codex.window_five_hour"), "seven_day": i18n.t("cli.codex.window_seven_day")}
    parts = [f"{names.get(n, n)} {round((w.get('used') or 0) * 100)}%"
             + (", " + i18n.t("cli.codex.window_resets", when=time.strftime("%d.%m %H:%M", time.localtime(w["resets"])))
                if w.get("resets") else "")
             for n, w in fact["windows"].items()]
    return "; ".join(parts) + (" — " + i18n.t("cli.codex.limit_reached") if fact["status"] == "rejected" else "")


def login_flow(state_dir, show, wait=None, home=None, logout="hub codex logout", again=False):
    """A sign-in by a device code: show(link, code) shows it to a person; -> (ok, email, plan, why). home — another
    Codex folder than the hub's (the evals' sign-in, an account of the Codex pool), logout — its sign-out command for the
    "already signed in" refusal; again — sign out first on the same app-server (a lost sign-in still reads as signed in
    from its old file)."""
    rpc = open_rpc(state_dir, home)
    try:
        if again:
            try:
                rpc.call("account/logout", {})
            except RuntimeError:
                pass
        try:
            had = account(rpc)
        except RuntimeError as e:   # a dead token: the old file is signed out (a clean folder), the sign-in goes on
            if not dead_token(e):
                raise
            try:
                rpc.call("account/logout", {})
            except RuntimeError:
                pass
            drop_sign_in(state_dir, home)
            rpc.close()
            rpc = open_rpc(state_dir, home)
            had = account(rpc)
        if had:
            return False, had[0], had[1], i18n.t("cli.codex.already_in", email=had[0], logout=logout)
        start = rpc.call("account/login/start", {"type": "chatgptDeviceCode"})
        show(start.get("verificationUrl"), start.get("userCode"))
        done = rpc.wait_note("account/login/completed", wait or float(os.environ.get("HUB_CODEX_LOGIN_WAIT", LOGIN_WAIT)))
        if not done:
            try:
                rpc.call("account/login/cancel", {"loginId": start.get("loginId")})
            except RuntimeError:
                pass
            return False, None, None, i18n.t("cli.codex.code_expired")
        if not done.get("success"):
            return False, None, None, done.get("error") or i18n.t("cli.codex.login_refused")
        got = account(rpc) or (None, None)
        return True, got[0], got[1], None
    finally:
        rpc.close()


USAGE = "cli.codex.usage"   # the catalog key of the usage line (the pool's commands have their own, codexslot)


def usage():
    """The usage lines of `hub codex`: the sign-in's, and the pool's where the tree has its code (core/codexslot.py)."""
    pool = _pool()
    return i18n.t(USAGE) + ("\n" + i18n.t(pool.USAGE) if pool else "")


def _pool():
    """The pool's code (core/codexslot.py), or None; an embed that does not import is said on stderr, the command goes on
    as on a tree without it."""
    try:
        return codexslot.code()
    except Exception as e:  # noqa: BLE001 — the embed's code, whatever it raises
        print(i18n.t("cli.codex.failed", what="pool", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
        return None


def main(rest, state_dir, me=None, put=None):
    what = rest[0] if rest else "status"
    pool = _pool() if what in ("add", "use", "remove") or what in ("login", "logout") and rest[1:] else None
    if pool:
        return pool.cli(what, rest[1:], state_dir, me, put)   # the Codex account pool (an embed's, core/codexslot.py)
    if what not in ("login", "status", "logout") or rest[1:]:
        print(usage(), file=sys.stderr)
        return 2
    if what in ("login", "logout") and me:
        print(i18n.t("cli.codex.owner_only_login", what=what))
        return 1
    try:
        if what == "status":
            rpc = Rpc(state_dir)
            try:
                got = account(rpc)
                if not got:
                    print(i18n.t("cli.codex.status_out"))
                    return 1
                print(i18n.t("cli.codex.status_in", email=got[0]) + (", " + i18n.t("cli.codex.plan", plan=got[1]) if got[1] else ""))
                try:
                    print(i18n.t("cli.codex.limits", line=windows_line(rpc.call("account/rateLimits/read", {}))))
                except RuntimeError as e:
                    print(i18n.t("cli.codex.limits_unread", error=e))
                return 0
            finally:
                rpc.close()
                pool = _pool()
                if pool:
                    try:
                        pool.status(state_dir)
                    except Exception as e:  # noqa: BLE001 — the sign-in's answer above stands
                        print(i18n.t("cli.codex.failed", what="status", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
        if what == "logout":
            logout_home(state_dir)
            put("codex.logout", None, {})
            return 0

        def show(url, code):
            print(i18n.t("cli.codex.open_code", url=url, code=code), flush=True)
        ok, email, plan, why = login_flow(state_dir, show)
        if not ok:
            print(i18n.t("cli.codex.not_in", why=why))
            return 1
        print(i18n.t("cli.codex.signed_as", email=email) + (", " + i18n.t("cli.codex.plan", plan=plan) if plan else ""))
        put("codex.login", None, {"email": email, "plan": plan})
        return 0
    except (OSError, RuntimeError) as e:
        print(i18n.t("cli.codex.failed", what=what, error=e), file=sys.stderr)
        return 1


# --- the Codex account pool's rows as the core took them (the state's codex.pool; its commands — core/codexslot.py) --
def pool_rows(state_dir, st=None):
    """{name: row} of the pool's rows the core took (the state's codex.pool)."""
    if st is None:
        from core import journal
        st = journal.load_state(state_dir)
    return {p["name"]: p for p in ((st.get("codex") or {}).get("pool") or {}).get("profiles") or []}


def pool_view(st, now=None):
    """The Codex pool's rows the core took, as the page and `hub codex status` show them: name, hub?, email, plan,
    signed (the folder has its sign-in file), active, lost, busy (until), limits (windows), state — no | lost | busy |
    active | free."""
    cx = st.get("codex") or {}
    acc, now = cx.get("account") or {}, time.time() if now is None else now
    out = []
    for p in (cx.get("pool") or {}).get("profiles") or []:
        name = p["name"]
        login = cx.get("login") if p.get("hub") else (acc.get("logins") or {}).get(name)
        busy = (acc.get("blocked") or {}).get(name)
        row = {"name": name, **({"hub": True} if p.get("hub") else {}), "email": (login or {}).get("email"),
               "plan": (login or {}).get("plan"), "signed": os.path.isfile(os.path.join(p["home"], "auth.json")),
               "active": name == acc.get("active"), "lost": bool((acc.get("lost") or {}).get(name)),
               "busy": busy if busy and busy > now else None, "limits": ((acc.get("seen") or {}).get(name) or {}).get("windows")}
        row["state"] = ("no" if not row["signed"] else "lost" if row["lost"] else "busy" if row["busy"] else
                        "active" if row["active"] else "free")
        out.append(row)
    return out


def account_fact(name, row, data):
    """The fact of a sign-in into a row: the hub folder's is codex.login with its row's name (a core without the pool
    takes it as the hub's — it is), an account folder's is codex.account_login."""
    if not name or (row or {}).get("hub"):
        return "codex.login", {**data, **({"account": name} if name else {})}
    return "codex.account_login", {**data, "account": name}


def logout_home(state_dir, home=None):
    """account/logout in a Codex folder (the hub's by default): one app-server conversation. A dead token (the
    app-server refuses the sign-out itself) still ends signed out: the folder's sign-in file is deleted. The network or
    the server out of reach is an error and the file stays."""
    rpc = open_rpc(state_dir, home)
    try:
        rpc.call("account/logout", {})
    except RuntimeError as e:
        if not dead_token(e):
            raise
        drop_sign_in(state_dir, home)
    finally:
        rpc.close()


def _config(state_dir):
    from core import catalog
    try:
        return catalog.load_config(state_dir)
    except (OSError, ValueError):
        return {}


# --- the models of the hub's Codex: model/list, kept as a file for the page, the settings and the executor pairs ----
MODELS_MAX_AGE = 6 * 3600   # s; the list changes with a codex update, not by the hour


def models_path(state_dir):
    return os.path.join(state_dir, "codex-models.json")


def _model(m):
    """One entry of model/list -> {id, title, efforts, effort, default} or None (hidden, or not a model name)."""
    if not isinstance(m, dict) or m.get("hidden") is True:
        return None
    mid = m.get("model") or m.get("id")
    if not isinstance(mid, str) or not MODEL_ID.match(mid):
        return None
    efforts = [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or [] if isinstance(e, dict)]
    title = m.get("displayName")
    return {"id": mid, "title": title if isinstance(title, str) and title.strip() else mid,
            "efforts": [e for e in efforts if isinstance(e, str) and MODEL_ID.match(e)],
            "effort": m.get("defaultReasoningEffort") if isinstance(m.get("defaultReasoningEffort"), str) else None,
            "default": m.get("isDefault") is True}


def fetch_models(state_dir, rpc=None):
    """`model/list` of the hub's Codex — no model call, no sign-in needed: [{id, title, efforts, effort, default}] in its
    order, hidden ones left out; written to <state>/codex-models.json (models() reads it)."""
    own = rpc is None
    rpc = rpc or Rpc(state_dir)
    try:
        out, seen, cursor = [], set(), None
        for _ in range(20):   # pages; the server's own size (8 models in one page, 0.162.1)
            res = rpc.call("model/list", {"cursor": cursor} if cursor else {})
            for m in map(_model, res.get("data") or []):
                if m and m["id"] not in seen:   # one model twice (two presets) — one row
                    seen.add(m["id"])
                    out.append(m)
            cursor = res.get("nextCursor")
            if not cursor:
                break
    finally:
        if own:
            rpc.close()
    if out:   # an empty answer does not wipe a list read before
        path = models_path(state_dir)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "models": out}, f, ensure_ascii=False)
        os.replace(tmp, path)
    return out


def models(state_dir):
    """(models, when read) of the hub's Codex from the file fetch_models wrote; (None, None) — not read yet or broken."""
    try:
        with open(models_path(state_dir), encoding="utf-8") as f:
            got = json.load(f)
        listed = [m for m in map(_back, got["models"]) if m]
        return (listed, float(got["ts"])) if listed else (None, None)
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def _back(m):
    """An entry of the file as fetch_models wrote it; anything else (a hand edit) is left out."""
    if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not MODEL_ID.match(m["id"]):
        return None
    efforts = m.get("efforts") if isinstance(m.get("efforts"), list) else []
    return {"id": m["id"], "title": m.get("title") if isinstance(m.get("title"), str) else m["id"],
            "efforts": [e for e in efforts if isinstance(e, str)], "default": m.get("default") is True,
            "effort": m.get("effort") if isinstance(m.get("effort"), str) else None}


# --- the sign-in from the page: a background process, the link and the code as a file ----------------------------
def login_dir(state_dir):
    return os.path.join(state_dir, "codex-login")


def page_status(state_dir):
    """Whether a sign-in from the page is going on: {info, pid} or the result {ok, why}; nothing — None."""
    out = {}
    for name in ("pid", "info", "result"):
        try:
            with open(os.path.join(login_dir(state_dir), name + ".json"), encoding="utf-8") as f:
                out[name] = json.load(f)
        except (OSError, ValueError):
            pass
    return out or None


def start_login(state_dir, wait=15.0, account=None, home=None, hub=False, again=False):
    """The page: "Sign in" starts a background sign-in; wait for the link and the code up to wait s. Already going — its
    link (of whichever account: one sign-in at a time). account/home — a row of the Codex account pool (hub — the hub
    folder's row); again — sign out in its folder first (a lost sign-in still reads as signed in from its old file)."""
    import platforms
    d = login_dir(state_dir)
    st = page_status(state_dir) or {}
    pid = (st.get("pid") or {}).get("pid")
    if not (pid and platforms.current.alive(pid) and "result" not in st):   # a sign-in is already going — wait for its link
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        more = [*(["--account", account] if account else []), *(["--home", home] if home else []),
                *(["--hub"] if hub else []), *(["--again"] if again else [])]
        with open(os.path.join(d, "stderr.log"), "a") as err:
            child = platforms.current.popen_detached([sys.executable, os.path.abspath(__file__), "wait", state_dir,
                                                      *more],
                                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        # its own file, at once: a second click (a second tab) does not start a second sign-in on top; only the sign-in writes info.json
        _write(state_dir, "pid", {"pid": child.pid, "ts": time.time(), "born": platforms.current.process_start(child.pid)})
    end = time.time() + wait
    while time.time() < end:
        st = page_status(state_dir) or {}
        if (st.get("info") or {}).get("code") or st.get("result"):
            return {**(st.get("info") or {}), **({"result": st["result"]} if st.get("result") else {})}
        time.sleep(0.1)
    return {"result": {"ok": False, "why": i18n.t("cli.codex.no_code")}}


def stop_login(state_dir):
    """Stop the page's sign-in still waiting for its code (the hub goes: `hub uninstall`) — its process and its codex
    app-server, which on Windows hold the files of the folder that is to be deleted. By the number and the start time
    start_login wrote; one written without a start time (an earlier version) is left alone. -> True when stopped."""
    import platforms
    st = page_status(state_dir) or {}
    if "result" in st:
        return False
    pid = (st.get("pid") or {}).get("pid")
    born = (st.get("pid") or {}).get("born")
    if not (isinstance(pid, int) and platforms.current.stop_detached(pid, born)):   # `hub uninstall`: this tree's
        return False
    end = time.time() + 5   # the folder is deleted next: Windows frees a stopped process's files when it has gone
    while platforms.current.alive(pid) and platforms.current.process_start(pid) == born and time.time() < end:
        time.sleep(0.1)
    return True


def _write(state_dir, name, data):
    path = os.path.join(login_dir(state_dir), name + ".json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def _wait(state_dir, account=None, home=None, hub=False, again=False):
    def show(url, code):
        _write(state_dir, "info", {"url": url, "code": code, "pid": os.getpid(), "ts": time.time(),
                                   **({"account": account} if account else {})})
    try:
        ok, email, plan, why = login_flow(state_dir, show, home=home, again=again)
    except (OSError, RuntimeError) as e:
        ok, email, plan, why = False, None, None, str(e)
    _write(state_dir, "result", {"ok": ok, "why": why, **({"account": account} if account else {})})
    if ok:   # from the page — the owner's word (the web channel)
        kind, data = account_fact(account, {"hub": True} if hub else {}, {"email": email, "plan": plan})
        spool.put(os.path.join(state_dir, "spool"), kind, None, {**data, "channel": "web", "owner": True})


def _wait_args(argv):
    out, i = {}, 0
    while i < len(argv):
        if argv[i] in ("--account", "--home") and i + 1 < len(argv):
            out[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            out[argv[i][2:]] = True
            i += 1
    return out


if __name__ == "__main__" and sys.argv[1:2] == ["wait"]:
    _wait(sys.argv[2], **_wait_args(sys.argv[3:]))
