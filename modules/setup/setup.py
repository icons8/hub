"""«Setup» — the checklist that takes a new hub to its first answer (installer spec §6.1, §9), for the band above the page while a
required item is red and for the cards of Settings → About the hub for what is not done:

| item   | checked by                                         | the hub does (modules/setup/job.py, its own process) |
|--------|----------------------------------------------------|------------------------------------------------------|
| claude | the release's own copy (or `claude --version` ≥)   | downloads it into the hub's folder (core/ownclaude)  |
| login  | `claude auth status`; a team pool signs in as well | `claude auth login`: a link, a code field             |
| chrome | the agents' browser finds a Chrome                 | Chrome for Testing as pinned (modules/setup/chrome)  |
| team   | git access to the team's modules                   | GitHub by device code with the hub's own gh          |
| doctor | `hub doctor`                                       | runs it                                              |

Codex (experimental): a hub entirely on Codex (hub.json "host": "codex") needs no Claude Code — its
first two items are `codex` (the program and `codex --version`, nothing installs it) and `codex_login` (the hub's Codex
sign-in in <state>/codex-home: the sign-in file — the same check spawns wait for — then `account/read` for the
account; a key in the environment is no sign-in for `codex app-server`; the page signs in through
core/codexcli.start_login, the same as «Settings → Codex»). Every codex probe runs with the hub's CODEX_HOME.

Required are only `claude` and `login` (on a Codex hub `codex` and `codex_login`; «ready» is both green); `chrome` and `team` never hold the hub. `team` is shown
only where the team's modules are in play (§9.1): a pool of accounts or another function that needs them, an earlier
failed access or the hub's own GitHub sign-in; without modules/private.json in the
code it does not exist at all.

The page only reads facts here; texts are its own (i18n). Probes of programs are cached: the band asks every couple of
minutes, and `claude auth status` starts a program."""
import os
import re
import shutil
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
JOB = os.path.join(HERE, "job.py")
REQUIRED = ("claude", "login")
APP_STEPS = ("claude", "claude_upgrade", "codex", "login", "chrome", "doctor", "voice")   # an app hub: no GitHub, no team's modules (§4a.3)
CODEX_REQUIRED = ("codex", "codex_login")   # Codex (experimental): a hub entirely on Codex
VERSION_TTL = 60
LOGIN_TTL_OK = 300   # s: a sign-in that is there is asked about again this rarely (a program start each time)
LOGIN_TTL = 20       # s: no sign-in yet — sooner (the person may have signed in from a terminal)
DOCTOR_FRESH = 600   # s: the page runs `hub doctor` itself when the last one is older
PROGRESS_FRESH = 60  # s: the layer's download writes its progress twice a second; an older file is a process that died
JOB_KEYS = ("step", "state", "started", "ts", "text", "why", "progress", "version", "path", "url", "local_url", "code_sent",
            "account", "code", "verify", "login", "signed", "access", "invite", "kind", "rows", "modules", "model", "lang")
CODE = re.compile(r"[\x21-\x7e]{1,2000}")   # a pasted authorization code: printable ASCII, one line


def _run(argv, timeout=30):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r


def is_app(repo=REPO):
    """The code is an app's (its PACKAGED says format "app", core/packaged.py): no wizard, the steps run by themselves."""
    import json
    from core import packaged
    if repo == REPO:
        return packaged.is_app(packaged.info())   # its MARK: a stand names its own (HUB_PACKAGED_MARK)
    try:
        with open(os.path.join(repo, "PACKAGED"), encoding="utf-8") as f:
            return packaged.is_app(json.load(f))
    except (OSError, ValueError):
        return False


class Setup:
    """state_dir, config() — hub.json as the page reads it; claude — the command (core/daemon.claude_command);
    spawn(argv, env) starts a step's process (tests: in-process or recorded)."""

    def __init__(self, state_dir, config, claude=None, clock=time.time, spawn=None, repo=REPO, app=None, screen=None):
        self.state_dir, self.config, self.clock, self.repo = state_dir, config, clock, repo
        # does this machine show a browser to a person (a server has none): tests give their own answer
        self.screen = screen or self._screen
        self.app = is_app(repo) if app is None else bool(app)   # an app: no wizard, its steps run by themselves (auto.py)
        self._claude = claude
        self.spawn = spawn or self._spawn
        self.cache, self.lock = {}, threading.Lock()

    @staticmethod
    def _screen():
        import platforms
        return platforms.current.has_screen(os.environ)

    # --- probes -----------------------------------------------------------------------------------------------------
    def claude(self):
        if self._claude:
            return self._claude
        from core.daemon import claude_command
        return claude_command()

    def _cached(self, key, ttl, fn):
        """fn() at most once per ttl seconds (ttl may depend on the answer); a step that started or ended since drops
        the cache — not its progress (a download rewrites its file twice a second)."""
        now, sig = self.clock(), self._job_sig()
        with self.lock:
            got = self.cache.get(key)
            if got and now < got[1] and got[2] == sig:
                return got[0]
        value = fn()
        span = ttl(value) if callable(ttl) else ttl
        with self.lock:
            self.cache[key] = (value, now + span, sig)
        return value

    def _job_sig(self):
        """(step, state, started) of every step: what changes when a step starts or ends. The files as they are — no
        process probes: a step that died without ending its file is caught by the next poll's own job()."""
        import json
        from modules.setup import job
        out = []
        for step in job.STEPS:
            try:
                with open(job.status_path(self.state_dir, step), encoding="utf-8") as f:
                    j = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(j, dict):
                out.append((step, j.get("state"), j.get("started")))
        return tuple(out)

    def claude_found(self):
        from core import daemon
        cmd = self.claude()[0]
        if hasattr(daemon, "claude_present"):   # the core asks the same before it spawns sessions
            return daemon.claude_present(cmd)
        import shutil   # a page process of the previous release holds the previous core.daemon in memory
        return os.path.isfile(cmd) or bool(shutil.which(cmd))

    def claude_version(self):
        def probe():
            r = _run(self.claude() + ["--version"], 60)
            m = re.search(r"\d+\.\d+\.\d+", r.stdout or "") if r is not None and r.returncode == 0 else None
            return m.group(0) if m else None
        return self._cached("version", VERSION_TTL, probe)

    # --- Codex (experimental): the same for a hub entirely on Codex ---------------------------------------------------
    def codex_program(self):
        """The codex the host spawns (hosts/codex.codex_bin) when it is there, or None."""
        from hosts.codex import codex_bin
        exe = codex_bin()
        return exe if os.path.isfile(exe) else shutil.which(exe)

    def codex_version(self):
        def probe():
            if not self.codex_program():
                return None
            from core import supportbundle   # with the hub's CODEX_HOME, not the personal ~/.codex
            line = supportbundle.codex_version(self.state_dir) or ""
            m = None if line.startswith("- (") else re.search(r"\d+\.\d+\.\d+", line)   # "- (…)": it failed
            return m.group(0) if m else None
        return self._cached("codex_version", VERSION_TTL, probe)

    def codex_file(self):
        from core import codexcli
        return os.path.join(codexcli.home_of(self.state_dir), "auth.json")

    def codex_signed(self):
        """Whether the sign-in file is there — core/daemon.codex_signed_in, which spawns of the hub wait for."""
        from core import daemon
        if hasattr(daemon, "codex_signed_in"):
            from core import codexcli   # the Codex pool: the active account's folder, as the spawns wait for
            home = codexcli.active_home(self.state_dir) if hasattr(codexcli, "active_home") else None
            return daemon.codex_signed_in(self.state_dir, home) if home else daemon.codex_signed_in(self.state_dir)
        return os.path.isfile(self.codex_file())   # a page process of a previous release: its core.daemon in memory

    def codex_account(self):
        """{email, plan} of the hub's Codex sign-in, {} — Codex does not take the sign-in file, {"error"} — app-server
        did not answer. One app-server start per LOGIN_TTL(_OK), and anew when the file changed (a sign-in, a refresh):
        the file's mtime is in the key — a probe that read the previous file stores under the previous key — and the
        entries of earlier files go."""
        try:
            stamp = os.stat(self.codex_file()).st_mtime_ns
        except OSError:
            stamp = None
        key = f"codex_account:{stamp}"
        with self.lock:
            for k in [k for k in self.cache if str(k).startswith("codex_account:") and k != key]:
                del self.cache[k]

        def probe():
            from core import codexcli
            try:
                rpc = codexcli.Rpc(self.state_dir)
            except (OSError, RuntimeError):
                return {"error": True}
            try:
                got = codexcli.account(rpc)
            except (OSError, RuntimeError):
                return {"error": True}
            finally:
                rpc.close()
            return {"email": got[0], "plan": got[1]} if got else {}
        return self._cached(key, lambda v: LOGIN_TTL_OK if v and not v.get("error") else LOGIN_TTL,
                            probe)

    def codex_job(self):
        """The sign-in from the page (core/codexcli.start_login: <state>/codex-login): running with the link and the code,
        done or failed with why; a «running» whose process is gone is a broken one. None — never started here."""
        from core import codexcli
        st = codexcli.page_status(self.state_dir)
        if not st:
            return None
        res = st.get("result") if isinstance(st.get("result"), dict) else None
        info = st.get("info") if isinstance(st.get("info"), dict) else {}
        pid = st.get("pid") if isinstance(st.get("pid"), dict) else {}
        if res:
            return {"state": "done" if res.get("ok") else "failed", "why": res.get("why")}
        # a device code lives ~15 min: a «running» older than that is a pid the OS has given to another process since
        try:
            life = float(os.environ.get("HUB_CODEX_LOGIN_WAIT", codexcli.LOGIN_WAIT))   # as the sign-in itself waits
        except ValueError:
            life = codexcli.LOGIN_WAIT
        fresh = self.clock() - (pid.get("ts") or 0) < life + 60
        if fresh and self._alive(pid.get("pid")):
            return {"state": "running", **{k: info[k] for k in ("url", "code") if isinstance(info.get(k), str)}}
        return {"state": "failed", "why": "gone"}

    def item_codex(self):
        """Codex CLI: there at least MIN_CODEX — ok. The app (§4a.1) installs it from OpenAI's releases (step codex):
        none, or an older one the hub put itself (own); an older one of npm, Homebrew or Codex's own installer is not
        installed over — the band says to update it there."""
        from core import codexinstall
        ver, path, j = self.codex_version(), self.codex_program(), self.job("codex")
        own = bool(path) and codexinstall.mine(path)
        # the hub's own copy without the helper codex starts for its tools is not done: every tool call of a session fails
        new = bool(ver) and codexinstall.newer_or_same(ver, codexinstall.MIN_CODEX) and \
            (not own or codexinstall.complete(path, state=self.state_dir))
        state = "ok" if new else "running" if (j or {}).get("state") == "running" else \
            "failed" if (j or {}).get("state") == "failed" or (path and not ver) else "todo"
        return {"id": "codex", "state": state, "version": ver, "path": path, "need": codexinstall.MIN_CODEX,
                "own": own, "found": bool(path), "job": j}

    def item_codex_login(self, codex_ok, working=False, full=True):
        """As item_login: working — sessions of the hub answered lately, then the band starts no app-server while the
        sign-in file is there. No file is red whatever answered before (the hub's spawns wait for it)."""
        j = self.codex_job()
        running = (j or {}).get("state") == "running"
        signed = self.codex_signed()
        if not codex_ok:
            return {"id": "codex_login", "state": "running" if running else "wait", "job": j}
        if signed and working and not full and not running:
            return {"id": "codex_login", "state": "ok", "via": "working", "job": j}
        if signed:
            acc = self.codex_account()
            if acc.get("error"):   # app-server did not answer: unknown — not red, the turns show their own failure
                return {"id": "codex_login", "state": "running" if running else "unknown", "job": j}
            if acc:
                return {"id": "codex_login", "state": "ok", "via": "own", "account": acc, "job": j}
        if running:
            return {"id": "codex_login", "state": "running", "job": j}
        return {"id": "codex_login", "state": "failed" if (j or {}).get("state") == "failed" else "todo", "job": j}

    def auth(self):
        from modules.setup import job
        return self._cached("auth", lambda v: LOGIN_TTL_OK if (v or {}).get("loggedIn") else LOGIN_TTL,
                            lambda: job.auth_status(self.claude()))

    def job(self, step):
        """The step's last state for the page; a «running» whose process is gone is a broken one."""
        import json
        from modules.setup import job
        try:
            with open(job.status_path(self.state_dir, step), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(st, dict):
            return None
        out = {k: st[k] for k in JOB_KEYS if k in st}
        if out.get("state") == "running" and not self._alive(st.get("pid")):
            out.update(state="failed", why="gone")
        return out

    def _alive(self, pid):
        import platforms
        return isinstance(pid, int) and pid > 0 and platforms.current.alive(pid)

    def cfg(self):
        try:
            return self.config() or {}
        except (OSError, ValueError):
            return {}

    # --- the items --------------------------------------------------------------------------------------------------
    def item_claude(self):
        """The release's own Claude Code (core/ownclaude) there — ok. Not yet (a working copy names none, the hub came up
        on this release before its download, HUB_CLAUDE): the claude it runs meanwhile, at least the version — ok with
        `pending` (auto.py downloads the own copy on an app hub, the update on the others), older or none — todo."""
        from core import ownclaude, version
        j, rel = self.job("claude"), ownclaude.release(self.repo)
        own = rel and not os.environ.get("HUB_CLAUDE")
        if own and ownclaude.mine(self.state_dir, rel):
            return {"id": "claude", "state": "ok", "version": rel, "need": rel, "job": j, "own": True}
        ver, need = self.claude_version(), rel or version.MIN_CLAUDE
        ok = bool(ver and version.parse_version(ver) >= version.parse_version(need))
        state = "ok" if ok else "running" if (j or {}).get("state") == "running" else \
            "failed" if (j or {}).get("state") == "failed" else "todo"
        return {"id": "claude", "state": state, "version": ver, "need": need, "job": j,
                **({"own": False, "pending": True} if own else {}),
                # a program whose --version stumbled: never put over on a hub without its own copy (auto.py)
                **({"found": self.claude_found()} if not ok and not ver else {})}

    def team_login(self, cfg):
        """The team's pool of accounts signs sessions in (core/profiles): the pool is there and its code is (built in, or
        the login module of the team's modules)."""
        from modules.update import private
        try:
            if not private.needed(cfg).get("accounts"):
                return False
        except Exception:  # noqa: BLE001 — an unreadable pool file is no sign-in
            return False
        if private.builtin_present("profile", self.repo):
            return True
        mods = (private.read_state(self.state_dir).get("modules") or {})
        return (mods.get("accounts") or {}).get("state") == "ready"

    def item_login(self, cfg, claude_ok, working=False, full=True):
        """working — sessions of this hub answered lately: their sign-in works whatever `claude auth status` of the
        page's process says (a key in the sessions' environment, a gateway); the band then never asks a working hub to
        sign in, and it does not start a program for that."""
        j = self.job("login")
        running = (j or {}).get("state") == "running"
        if self.team_login(cfg):
            return {"id": "login", "state": "ok", "via": "team", "job": j}
        if not claude_ok:
            return {"id": "login", "state": "running" if running else "wait", "job": j}
        if working and not full and not running:
            return {"id": "login", "state": "ok", "via": "working", "job": j}
        st = self.auth()
        if st and st.get("loggedIn"):
            return {"id": "login", "state": "ok", "via": "own",
                    "account": {k: st[k] for k in ("email", "authMethod", "orgName") if k in st}, "job": j}
        if running:
            return {"id": "login", "state": "running", "job": j}
        if working:
            return {"id": "login", "state": "ok", "via": "working", "job": j}
        if st is None:   # an older Claude Code or a crash: unknown — not red, sessions show their own sign-in trouble
            return {"id": "login", "state": "unknown", "job": j}
        return {"id": "login", "state": "failed" if (j or {}).get("state") == "failed" else "todo", "job": j}

    def item_chrome(self, cfg):
        from modules.browser import browsers
        from modules.setup import chrome
        from modules.testchrome import chrome as testchrome
        j = self.job("chrome")
        try:
            path = browsers.find_chrome(cfg)
            if path and os.path.exists(path) and testchrome.MAC:
                # the machine's own Google Chrome serves the agents' browser, but never `hub shot` and diagrams on macOS
                # (a dock icon: testchrome.find): the row is done only with a Chrome those take — else the hub downloads its own
                path = testchrome.find(cfg)
            if path and os.path.exists(path):
                return {"id": "chrome", "state": "ok", "path": path, "job": j}
        except browsers.Problem:
            pass
        pin = chrome.pinned()
        state = (j or {}).get("state")
        if pin and chrome.sandbox_blocked():   # Ubuntu 23.10+: a Chrome in the user's folder cannot start (chrome.install)
            package = chrome.sandbox_command()
            return {"id": "chrome", "state": "todo", "available": True, "sandbox": package or "",
                    **({} if package else self.system_chrome(chrome)), "job": j}
        out = {"id": "chrome", "state": "running" if state == "running" else "failed" if state == "failed" else "todo",
               "available": bool(pin), "mb": round(pin["size"] / 1e6) if pin else None, "job": j}
        if state == "failed" and (j or {}).get("why") == "start":   # it came, it does not start: libraries a server lacks
            out.update(self.system_chrome(chrome))
        return out

    @staticmethod
    def system_chrome(chrome):
        """{"system": the root command that puts Google Chrome on the machine} where there is one."""
        cmd = chrome.system_command()
        return {"system": cmd} if cmd else {}

    def item_team(self, cfg):
        from core import ghauth
        from modules.update import private
        if self.app:   # the app takes its code from our server and has no team's modules (app spec §4a.3)
            return None
        pin = private.load_pin(self.repo)
        if not pin:
            return None
        j = self.job("github") or None
        a = self.job("access")
        if a and (not j or (a.get("ts") or 0) >= (j.get("ts") or 0)):
            j = a
        prev = private.read_state(self.state_dir)
        acc = prev.get("access") if isinstance(prev.get("access"), dict) else {}
        try:
            need = any(private.needed(cfg).values())
        except Exception:  # noqa: BLE001
            need = False
        signed = ghauth.signed_in(self.state_dir)
        visible = bool(need or signed or j or acc.get("ok") is False)
        if (j or {}).get("state") == "running":
            state = "running"
        elif acc.get("ok") and prev.get("state") == "done" or (j or {}).get("access") == "ok":
            state = "ok"
        elif (j or {}).get("state") == "failed":
            state = "failed"
        else:
            state = "todo"
        return {"id": "team", "state": state, "visible": visible, "signed": signed,
                "login": ghauth.login(self.state_dir) if signed else None, "gh": bool(ghauth.gh_program()),
                "invite": f"https://github.com/{pin['repo']}/invitations", "job": j}

    def item_doctor(self):
        j = self.job("doctor")
        rows = (j or {}).get("rows") or []
        state = (j or {}).get("state")
        level = "running" if state == "running" else "todo" if not j else \
            "fail" if any(r[0] == "fail" for r in rows) or (state == "failed" and not rows) else \
            "warn" if any(r[0] == "warn" for r in rows) else "ok"
        return {"id": "doctor", "state": level, "rows": rows, "ts": (j or {}).get("ts"),
                "stale": not j or self.clock() - ((j or {}).get("ts") or 0) > DOCTOR_FRESH, "job": j}

    def status(self, local=False, full=True, working=False):
        """{ready, blocking, local, items, auto}: full=False — only what the band needs (claude and login); working —
        sessions of the hub answered lately (item_login). An app hub (app spec §4a) always gives the whole list — its
        band is the only view of the setup — with app: true, the layer's download (update) and the steps auto.py waits
        to repeat (waits); auto stays empty there: auto.py starts the steps, not the page."""
        cfg = self.cfg()
        full = full or self.app
        codex = cfg.get("host") == "codex"   # Codex (experimental): claude is not needed — never probed, never installed
        if codex:
            c = self.item_codex()
            items = [c, self.item_codex_login(c["state"] == "ok", working, full)]
        else:
            c = self.item_claude()
            items = [c, self.item_login(cfg, c["state"] == "ok", working, full)]
        if full:
            items += [self.item_chrome(cfg)]
            team = self.item_team(cfg)
            if team:
                items.append(team)
            items.append(self.item_doctor())
        required = CODEX_REQUIRED if codex else REQUIRED
        blocking = [i["id"] for i in items if i["id"] in required and i["state"] not in ("ok", "unknown")]
        # «Claude Code downloads by itself» (§6 step 7): a new hub — no claude program at all, no session ever answered,
        # never tried here; a working hub whose `claude --version` stumbled once is never reinstalled behind its back
        auto = ["claude"] if not codex and not self.app and c["state"] == "todo" and not c["job"] and not working \
            and not c["version"] and not self.claude_found() else []
        # local: the request came from this machine; screen: it has a screen. A server opened through an SSH tunnel
        # is local without a screen: the browser is on the person's computer, the sign-in goes by Anthropic's code
        # only a local request needs it, and a service asks systemd and loginctl for it: once a minute, not every poll
        screen = bool(self._cached("screen", 60, self.screen)) if local else None
        out = {"ready": not blocking, "blocking": blocking, "local": bool(local), "screen": screen, "items": items,
               "auto": auto}
        if self.app:
            alive, waits = self.auto_state()
            out.update(app=True, update=self.layer_progress(), waits=waits, steps_running=alive,
                       host="codex" if codex else "claude")
        return out

    def layer_progress(self):
        """What the layer's download is doing now (modules/update/layer.py: layers/progress.json): {version, phase
        download|check, done, total, first} while it is fresh, else None (a process that died leaves an old file)."""
        import json
        try:
            with open(os.path.join(self.state_dir, "layers", "progress.json"), encoding="utf-8") as f:
                got = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(got, dict) or got.get("phase") not in ("download", "check"):
            return None
        ts = got.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or abs(self.clock() - ts) > PROGRESS_FRESH:
            return None
        num = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None   # noqa: E731
        return {"version": str(got.get("version") or "")[:64], "phase": got["phase"], "done": num(got.get("done")),
                "total": num(got.get("total")), "first": got.get("first") is True}

    def auto_state(self):
        """(auto.py runs, {step: when it starts that step again}) — a step that failed for the network waits so, while
        auto.py runs (modules/setup/auto.py: auto.json); not running — the band offers the step's button itself."""
        import json
        from modules.setup import job
        try:
            with open(os.path.join(job.folder(self.state_dir), "auto.json"), encoding="utf-8") as f:
                got = json.load(f)
        except (OSError, ValueError):
            return False, {}
        if not isinstance(got, dict) or not self._alive(got.get("pid")):
            return False, {}
        waits = got.get("waits") if isinstance(got.get("waits"), dict) else {}
        return True, {k: v for k, v in waits.items() if k in job.STEPS and isinstance(v, (int, float))
                      and not isinstance(v, bool)}

    # --- actions ----------------------------------------------------------------------------------------------------
    def _spawn(self, argv, env):
        from core import perms
        import platforms
        os.makedirs(os.path.join(self.state_dir, "logs"), exist_ok=True)
        with open(os.path.join(self.state_dir, "logs", "setup.log"), "a", encoding="utf-8") as log:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                                 cwd=self.state_dir, close_fds=True, umask=perms.user_umask(), **platforms.current.apart())
        threading.Thread(target=p.wait, daemon=True, name=f"setup-{argv[-1]}").start()   # reaped: no zombie in the page

    def run(self, step, lang=None, arg=None):
        """Start a step (a second press while it runs — the same one). arg — the voice step's model (job.ARG), given
        to its process: the step's lock makes the one that runs the one whose model it shows. -> (code, body)."""
        from modules.setup import job
        step = "github" if step == "team" else step
        if step not in job.STEPS or self.app and step not in APP_STEPS:
            return 400, {"error": "step"}
        if arg is not None and (step != "voice" or not job.ARG.fullmatch(str(arg))):
            return 400, {"error": "arg"}
        cur = self.job(step)
        if cur and cur.get("state") == "running":
            return 200, {"ok": True, "step": step, "already": True, **({"model": cur["model"]} if cur.get("model") else {})}
        if step == "voice":   # a start of its own: an earlier «Turn off» or «Cancel» left no mark that would stop it
            from modules.voice import install
            install.clear_off(self.state_dir)
        env = dict(os.environ, HUB_STATE_DIR=self.state_dir, **({"HUB_LANG": lang} if lang else {}))
        self.spawn([sys.executable, JOB, self.state_dir, step, *([arg] if arg else [])], env)
        with self.lock:
            self.cache.clear()
        return 202, {"ok": True, "step": step}

    def give(self, step, text):
        """The code the person pasted, to the running sign-in (`<step>.in`, 0600: a one-time code)."""
        import tempfile
        import platforms
        from modules.setup import job
        text = text.strip() if isinstance(text, str) else ""
        if step != "login" or not CODE.fullmatch(text):
            return 400, {"error": "code"}
        cur = self.job(step)
        if not cur or cur.get("state") != "running":
            return 409, {"error": "not running"}
        path = os.path.join(job.folder(self.state_dir), step + ".in")
        fd, tmp = tempfile.mkstemp(dir=job.folder(self.state_dir), prefix=step + ".in.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            platforms.current.replace(tmp, path)   # Windows: waits while the step reads the previous one
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return 202, {"ok": True}

    def codex_sign_in(self):
        """The hub's Codex sign-in by a device code (core/codexcli.start_login, as «Settings → Codex»): its code and link
        go to the band (codex_job). One at a time: a second call while one goes on is that one."""
        from core import codexcli
        with self.lock:
            self.cache.clear()
        return codexcli.start_login(self.state_dir, wait=0)

    def cancel(self, step):
        from modules.setup import job
        if step not in job.STEPS:
            return 400, {"error": "step"}
        cur = self.job(step)
        if not cur or cur.get("state") != "running":
            return 200, {"ok": True}
        with open(os.path.join(job.folder(self.state_dir), step + ".cancel"), "w", encoding="utf-8"):
            pass
        return 202, {"ok": True}


# string literals: the catalog checks (tools/i18n.py) find the keys the code asks for by them
TITLES = {"claude": "cli.setup.item_claude", "login": "cli.setup.item_login", "chrome": "cli.setup.item_chrome",
          "team": "cli.setup.item_team", "doctor": "cli.setup.item_doctor", "codex": "cli.setup.cx_g_codex",
          "codex_login": "cli.setup.cx_g_codex_login"}


def cli(state, claude):
    """`hub setup`: the checklist in the terminal. -> 0 when the required items are green."""
    from core import catalog, i18n
    s = Setup(state, lambda: catalog.config_or_last(state), claude=claude)
    st = s.status()
    marks = {"ok": "ok", "running": "…", "wait": "·", "unknown": "?", "warn": "warn", "fail": "fail",
             "failed": "fail", "todo": "—"}
    for it in st["items"]:
        if it["id"] == "team" and not it["visible"]:
            continue
        j = it.get("job") or {}
        detail = {"claude": it.get("version") or "", "login": (it.get("account") or {}).get("email") or
                  ("team" if it.get("via") == "team" else ""), "chrome": it.get("path") or it.get("sandbox") or "",
                  "team": it.get("login") or "", "doctor": "", "codex": it.get("version") or "",
                  "codex_login": (it.get("account") or {}).get("email") or ""}.get(it["id"], "")
        why = j.get("text") or (j.get("why") if it["id"] == "codex_login" else None)
        if why == "gone":
            why = i18n.t("web.setup.gone")
        if it["state"] in ("failed", "fail") and why:
            detail = (detail + " " + str(why)).strip()
        print(f"{marks.get(it['state'], it['state']):<5} {i18n.t(TITLES[it['id']])}" +
              (f"  {detail}" if detail else ""))
    print(voice_line(state))   # optional: never holds the hub
    print(i18n.t("cli.setup.ready" if st["ready"] else "cli.setup.not_ready_app"))
    return 0 if st["ready"] else 1


def voice_cli(state, args):
    """`hub setup voice [on [<model>]|off]`: the built-in voice (modules/voice) — on downloads the engine and the model
    (the named one of the hub language's rows, or the recommended one) in the step's own process (the page sees its
    progress), another model while voice is on — a switch: the model in use works until the new one passes its check;
    off removes them; no argument — the state and the rows. -> exit code."""
    from core import catalog, i18n
    from modules.voice import install
    if not (args in ([], ["on"], ["off"]) or (len(args) == 2 and args[0] == "on")):
        print("hub setup voice [on [<model>]|off]", file=sys.stderr)
        return 2
    s = Setup(state, lambda: {})

    def wait(since, limit=VOICE_WAIT):
        """Until the step is over: its file says running, or a step started a moment ago has not written it yet."""
        end = time.monotonic() + limit
        while time.monotonic() < end:
            j = s.job("voice") or {}
            if j.get("state") != "running" and (j.get("started") or 0) >= since - 1:
                return j
            if j.get("state") != "running" and time.time() > since + 10:
                return j
            time.sleep(1)
        return s.job("voice") or {}
    if args == ["off"]:
        install.mark_off(state)
        if (s.job("voice") or {}).get("state") == "running":
            s.cancel("voice")
            wait(0)   # the step stops and removes what came
        install.remove(state)
        print(i18n.t("cli.setup.voice_off"))
        return 0
    cfg = catalog.config_or_last(state) or {}
    st = install.status(state, i18n.current(), cfg=cfg)
    want = args[1] if len(args) == 2 else None
    if args and st.get("why") and not st.get("on"):   # no ffmpeg, no engine build, no model for the language: say so
        print(voice_line(state))
        return 1
    if want and want not in [m["id"] for m in st.get("models") or []]:
        print(i18n.t("cli.setup.voice_no_model", model=want), file=sys.stderr)
        print(voice_models(st), file=sys.stderr)
        return 2
    if args and args[0] == "on" and (not st.get("on") or st.get("missing") or (want and want != st.get("model"))):
        if (s.job("voice") or {}).get("state") == "running":   # the page's step: its model, not ours — say so
            print(i18n.t("web.voicehelp.busy"), file=sys.stderr)
            return 1
        print(i18n.t("cli.setup.voice_starting"), flush=True)
        install.clear_off(state)
        since = time.time()
        # job.py ends at once when the step already runs (the page started it): then that one is waited for
        subprocess.run([sys.executable, JOB, state, "voice", *([want] if want else [])],
                       env=dict(os.environ, HUB_STATE_DIR=state), stdin=subprocess.DEVNULL)
        j = wait(since)
        if j.get("state") != "done":
            print(i18n.t("cli.setup.voice_failed", why=j.get("text") or j.get("why") or j.get("state") or "?"))
            return 1
        st = install.status(state, i18n.current(), cfg=cfg)
    print(voice_line(state))
    if not args and st.get("models"):
        print(voice_models(st))
    return 0


ROLES = {"recommended": "cli.setup.voice_role_recommended", "lighter": "cli.setup.voice_role_lighter",
         "faster": "cli.setup.voice_role_faster", "accurate": "cli.setup.voice_role_accurate",
         "more": "cli.setup.voice_role_more"}


def voice_models(st):
    """The rows of the hub language for `hub setup voice`: the model in use marked *, the role of each."""
    from core import i18n
    lines = [i18n.t("cli.setup.voice_models")]
    for m in st.get("models") or []:
        mark = "*" if st.get("on") and m["id"] == st.get("model") else " "
        if m.get("cloud"):   # the cloud row (the team flavor): its facts in the hub's words, no size
            lines.append(f"{mark} {m['id']:<34} {m['name']:<26} {m.get('note') or ''}")
            continue
        role = i18n.t(ROLES[m["role"]]) if m.get("role") in ROLES else ""
        lines.append(f"{mark} {m['id']:<34} {m['name']:<26} {m['size'] / 1e6:>5.0f} MB  " +
                     i18n.t("cli.setup.voice_numbers_chars" if m.get("lang") in ("zh", "ja", "ko") else "cli.setup.voice_numbers",
                            errors=_num(m.get("error")), seconds=_num(m.get("wait30"))) +
                     (f"  {role}" if role else ""))
    lines.append("hub setup voice on [<model>]   hub setup voice off")
    return "\n".join(lines)


def _num(x):
    return "?" if not isinstance(x, (int, float)) else f"{x:.0f}" if x >= 10 else f"{x:.1f}".rstrip("0").rstrip(".")


VOICE_WAIT = 3600   # s: `hub setup voice` waits for its step no longer (a stalled download)


def voice_line(state):
    """The voice line of `hub setup`: on (the model), or off with what turning it on would take, or why not here."""
    from core import catalog, i18n
    from modules.voice import install
    from modules.voice import transcribe as voice
    cfg = catalog.config_or_last(state) or {}
    if voice.command(cfg):
        return i18n.t("cli.setup.voice_line_cmd")
    st = install.status(state, i18n.current(), cfg=cfg)
    if st.get("on"):
        return i18n.t("cli.setup.voice_line_on", model=st.get("name") or st.get("model") or "?")
    if st.get("why") == "lang":
        return i18n.t("cli.setup.voice_line_no_lang")
    if st.get("why") == "platform":
        return i18n.t("cli.setup.voice_line_no_platform")
    if st.get("why") == "ffmpeg":
        return i18n.t("cli.setup.voice_line_no_ffmpeg")
    return i18n.t("cli.setup.voice_line_off", name=st.get("name"), mb=f"{st.get('size', 0) / 1e6:.0f}")
