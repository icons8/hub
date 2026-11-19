"""`hub run` in the daemon (core/hubrun.py — the fold, platforms/runwrap.py — the wrapper): every tick the runs in the
state are matched against their folders <state>/runs/<id>/; nothing depends on the core's memory, so a new life of
hubd after a restart picks up where the previous one stopped.

  queued    no folder — start: wrap.py, then spec.json, then the OS call (a run apart from the hub: Linux under systemd a
            unit of its own in the runs' slice, Windows by WMI outside every job of the hub, else a session of its own).
            The wrapper takes spec.json by rename: taken — core.run_started; not taken in START_WAIT — the core cancels
            by its own rename (one of the two wins) and records not_started. A failed OS call is not_started too, never a
            run inside the hub (it would die with the hub's restart).
  running   result.json — core.run_ended; the wrapper's life lock free and no result — what is left is stopped first,
            then core.run_ended lost (the fact last: a crash between the two repeats the step).
  stopping  the `stop` file for the wrapper; no outcome in HARD_AFTER — stop it hard, then the same order as for lost.

Slow steps (the OS call, waiting for the wrapper, a unit stop) go in a thread per run; the tick records their facts.
The folders of runs that ended KEEP_DAYS ago, over ENDED_BYTES in all, or with no record at all, are removed."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import platforms
from core import catalog, perms, profiles
from platforms import runwrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDER = "runs"
START_WAIT = 60      # seconds for the wrapper to take its spec after the OS call
HARD_AFTER = 30      # seconds after the stop request before the run is stopped hard
EVERY = {"queued": 1, "stopping": 1, "running": 10}   # seconds between looks at a run in this state
TAIL_LINES, TAIL_CHARS = 40, 3000
KEEP_DAYS = 7
ENDED_BYTES = 2 * 1024 ** 3
SWEEP_EVERY = 3600
GRACE = runwrap.GRACE


def folder_of(state_dir, rid):
    return os.path.join(state_dir, FOLDER, rid)


def space(state_dir):
    """A short mark of this hub for the names of its systemd units: two hubs on one machine do not share them."""
    return hashlib.sha1(os.path.realpath(state_dir).encode()).hexdigest()[:8]


def tail(path, lines=TAIL_LINES, chars=TAIL_CHARS):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - chars * 4))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])[-chars:]


class Runner:
    """The daemon's side of `hub run`. record(type, entity, data) writes a fact; run(argv) runs a program (a test
    replaces it); launch_apart(argv, env) starts the wrapper apart from the hub -> (how, unit, error)."""

    def __init__(self, state_dir, record, clock=time.time, run=None, launch_apart=None, env=None, platform=None):
        self.state_dir, self.record, self.clock = state_dir, record, clock
        self.run = run or (lambda argv, **kw: subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                                             text=True, timeout=60, **kw))
        self.env = os.environ if env is None else env
        self.platform = platform or platforms.current
        self.launch_apart = launch_apart or self._launch_apart
        self.jobs = {}      # run id -> {"thread", "done", "fact": (type, data) or None}
        self.looked = {}    # run id -> when it was last looked at
        self.swept = None
        self.slice_set = False

    # ---- the tick ---------------------------------------------------------------------------------------------------
    def tick(self, st):
        now = self.clock()
        runs = st.get("hub_runs") or {}
        recorded = set()   # st is the state before this tick's facts: a run they moved is looked at next tick
        for rid, job in list(self.jobs.items()):
            if job["done"].is_set():
                del self.jobs[rid]
                if job["fact"] and (runs.get(rid) or {}).get("state") in ("queued", "running", "stopping"):
                    self.record(job["fact"][0], rid, job["fact"][1])
                    recorded.add(rid)
        ended_now = False
        for rid, run in list(runs.items()):
            if run["state"] not in ("queued", "running", "stopping") or rid in self.jobs or rid in recorded:
                continue
            if now - self.looked.get(rid, 0) < EVERY[run["state"]]:
                continue
            self.looked[rid] = now
            fact = self.look(st, run, now)
            if fact:
                self.record(fact[0], rid, fact[1])
                ended_now |= fact[0] == "core.run_ended"
        self.looked = {k: v for k, v in self.looked.items() if k in runs}
        if ended_now or self.swept is None or now - self.swept >= SWEEP_EVERY:
            self.sweep(st, now)

    def look(self, st, run, now):
        """What a run's folder says -> a fact to record now, or None (nothing yet, or a thread took it)."""
        d = folder_of(self.state_dir, run["id"])
        p = lambda name: os.path.join(d, name)   # noqa: E731
        taken, cancelled = os.path.exists(p(runwrap.TAKEN)), os.path.exists(p(runwrap.CANCELLED))
        if cancelled:
            return "core.run_ended", {"code": None, "reason": "stopped" if run["state"] == "stopping" else "not_started",
                                      "error": self._said(d) or "the wrapper did not start", "log": p(runwrap.LOG)}
        if run["state"] == "queued" and not taken:
            if not os.path.exists(p(runwrap.SPEC)):
                self.thread(run["id"], lambda: self.start(st, run))
                return None
            if now - _mtime(p(runwrap.SPEC)) >= START_WAIT:
                return self.cancel(d, "not_started", "the wrapper did not take the run in time")
            return None
        if run["state"] == "stopping" and not taken:
            if os.path.exists(p(runwrap.SPEC)):
                return self.cancel(d, "stopped", None, by=run.get("stop_by"))
            return "core.run_ended", {"code": None, "reason": "stopped", "by": run.get("stop_by")}
        if os.path.exists(p(runwrap.RESULT)):
            return self.ended(run, d)
        if not os.path.exists(p(runwrap.PIDS)):
            if runwrap.alive(d):
                return None   # taken, the command is starting
            return self.ended(run, d) if os.path.exists(p(runwrap.RESULT)) else (
                "core.run_ended", {"code": None, "reason": "lost", "leftover": "none", "log": p(runwrap.LOG)})
        if run["state"] == "queued":   # taken, the start thread's own fact lost with a previous life of hubd
            return "core.run_started", {**self.started_data(d), **self.launched(d, run)}
        if runwrap.alive(d):
            if run["state"] == "stopping":
                if not os.path.exists(p(runwrap.STOP)):
                    runwrap.write_json(p(runwrap.STOP), {"by": run.get("stop_by") or "core"})
                elif now - _mtime(p(runwrap.STOP)) >= HARD_AFTER:
                    self.thread(run["id"], lambda: ("core.run_ended", self.clean(run, d, hard=True)))
            return None
        if os.path.exists(p(runwrap.RESULT)):   # the outcome is written before the exit: read again
            return self.ended(run, d)
        self.thread(run["id"], lambda: ("core.run_ended", self.clean(run, d, hard=False)))
        return None

    def thread(self, rid, work):
        job = {"done": threading.Event(), "fact": None}

        def go():
            try:
                job["fact"] = work()
            except Exception as e:  # noqa: BLE001 — the background is not silent: the run gets the reason
                job["fact"] = ("core.run_ended", {"code": None, "reason": "lost", "error": repr(e)[:300]})
            job["done"].set()
        job["thread"] = threading.Thread(target=go, name=f"hub-run-{rid}", daemon=True)
        self.jobs[rid] = job
        job["thread"].start()

    # ---- start ------------------------------------------------------------------------------------------------------
    def command_env(self, run, st):
        """As a session job's pre-check of the schedule has it (modules/scheduler/clock.Clock.env), on behalf of the
        run's session; without the session's tree mark, so the session's end does not take it. st is the core state: with
        the account gateway on, the run's `claude -p` goes through it (core/profiles.job_env)."""
        env = {**self.platform.owner_env(dict(self.env)), **catalog.user_env(), "HUB_STATE_DIR": self.state_dir,
               "PATH": os.path.join(REPO, "bin") + os.pathsep + self.env.get("PATH", ""),
               "HUB_SESSION": run["session"], "HUB_RUN": run["id"]}
        for k in (platforms.TREE_ENV, "HUB_SCHEDULE_JOB", "HUB_EXTENSION", "HUB_MODULE"):
            env.pop(k, None)
        return profiles.job_env(env, self.state_dir, st)

    def argv_of(self, run, env):
        """bash -c <command>, as the Bash tool: Git Bash on Windows; no bash — None (no other shell: another language)."""
        if self.platform.APP_OS == "windows":
            args, shell, env = self.platform.job_shell(run["command"], env, self.state_dir, "bash")
            return (args, env) if not shell else (None, env)
        bash = shutil.which("bash", path=env.get("PATH"))
        return ([bash, "-c", run["command"]], env) if bash else (None, env)

    def start(self, st, run):
        d = folder_of(self.state_dir, run["id"])
        # exist_ok: left by a life of hubd before spec.json — no OS call was made; private (Windows: no 0o700, its ACL
        # would cut off the owner's terminal — platforms makedirs_private)
        self.platform.makedirs_private(os.path.dirname(d))
        self.platform.makedirs_private(d)
        shutil.copy(runwrap.__file__, os.path.join(d, "wrap.py"))
        env = self.command_env(run, st)
        argv, env = self.argv_of(run, env)
        if argv is None:   # no spec.json is written: nothing can start it
            return "core.run_ended", {"code": None, "reason": "not_started", "error": "bash not found"}
        if not os.path.isdir(run["cwd"]):   # never another folder: a relative `rm` would hit what it was not meant for
            return "core.run_ended", {"code": None, "reason": "not_started",
                                      "error": f"the folder {run['cwd']} is not there"}
        mode = self.mode(run)
        spec = {"argv": argv, "cwd": run["cwd"], "env": env,
                "until_at": run["until_at"], "umask": perms.user_umask(), "grace": GRACE, **mode}
        tmp = os.path.join(d, runwrap.SPEC + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(spec, f)
        os.replace(tmp, os.path.join(d, runwrap.SPEC))   # before the OS call: a new life of hubd never calls it twice
        try:
            how, unit, error = self.launch_apart([sys.executable, "-I", os.path.join(d, "wrap.py"), d], run)
        except Exception as e:  # noqa: BLE001 — the OS call may have started it all the same (a timeout of systemd-run)
            how, unit, error = mode["how"], mode.get("unit"), repr(e)[:300]
        if error:
            return self.cancel(d, "not_started", error)   # taken meanwhile — None: the next look picks it up
        end = self.clock() + START_WAIT
        while self.clock() < end:
            if os.path.exists(os.path.join(d, runwrap.PIDS)):
                return "core.run_started", {**self.started_data(d), "how": how, **({"unit": unit} if unit else {})}
            if os.path.exists(os.path.join(d, runwrap.TAKEN)) and not runwrap.alive(d):
                if os.path.exists(os.path.join(d, runwrap.RESULT)):
                    return "core.run_started", {"how": how, **({"unit": unit} if unit else {})}   # ended at once
                return "core.run_ended", {"code": None, "reason": "lost", "leftover": "none"}
            time.sleep(0.2)
        if os.path.exists(os.path.join(d, runwrap.TAKEN)):
            return "core.run_started", {"how": how, **({"unit": unit} if unit else {})}
        return self.cancel(d, "not_started", "the wrapper did not take the run in time")

    def mode(self, run):
        """How this hubd starts the run apart -> {"how", "unit"?}: written into spec.json before the OS call, so a
        later life of hubd (maybe under another service) picks it up and cleans it up the way it was started."""
        how = self.platform.run_apart_how(self.env)
        return {"how": how, **({"unit": f"hub-run-{space(self.state_dir)}-{run['id']}"} if how == "unit" else {})}

    def launched(self, d, run):
        """The mode the run was started with (spec.json, as taken or cancelled), else the run's record."""
        for name in (runwrap.TAKEN, runwrap.SPEC, runwrap.CANCELLED):
            spec = runwrap.read_json(os.path.join(d, name))
            if spec and spec.get("how"):
                return {k: spec[k] for k in ("how", "unit") if spec.get(k)}
        return {k: run[k] for k in ("how", "unit") if run.get(k)}

    def started_data(self, d):
        pids = runwrap.read_json(os.path.join(d, runwrap.PIDS)) or {}
        out = {}
        if isinstance(pids.get("pgid"), int) and self.platform.run_apart_how(self.env) == "session":
            # the group's leader and its start time: the cleanup after a lost wrapper signals only a verified group
            born, boot = self.platform.process_start(pids["pgid"]), self.platform.boot_id()
            out.update(pgid=pids["pgid"], **({"born": born} if born is not None else {}),
                       **({"boot": boot} if boot else {}))
        return out

    def _launch_apart(self, argv, run):
        """-> (how, unit or None, error or None)."""
        mode = self.mode(run)
        if mode["how"] == "wmi":
            return ("wmi", None, None) if self.platform.popen_outside(argv) else \
                ("wmi", None, "Windows did not start it outside the hub (WMI Win32_Process.Create)")
        if mode["how"] == "unit":
            unit, slice_ = mode["unit"], f"hub-runs-{space(self.state_dir)}.slice"
            if not self.slice_set:
                # all runs of the hub together: at most half of the machine's memory. Once a life of hubd, again after a
                # refusal; a refusal or a hung manager is only a line in the log — the run still goes
                try:
                    r = self.run(["systemctl", "--user", "set-property", "--runtime", slice_, "MemoryMax=50%"])
                    self.slice_set = r.returncode == 0
                    why = (r.stderr or r.stdout or f"code {r.returncode}").strip()[:200]
                except (OSError, subprocess.SubprocessError) as e:
                    why = repr(e)[:200]
                if not self.slice_set:
                    print(f"hub run: the memory cap of {slice_} was refused: {why}", flush=True)
            length = max(60, int(run["until_at"] - self.clock()))
            r = self.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", f"--slice={slice_}",
                          "-p", "OOMPolicy=continue", "-p", f"RuntimeMaxSec={length + 60}",
                          "-p", "TimeoutStopSec=15", "--", *argv])
            if r.returncode != 0:
                return "unit", unit, ("systemd-run: " + (r.stderr or r.stdout or f"code {r.returncode}").strip())[:300]
            return "unit", unit, None
        self.platform.popen_detached(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        return "session", None, None

    def cancel(self, d, reason, error, by=None):
        """The core's side of the one rename: won — the run never starts; lost — the wrapper has it, look again."""
        try:
            os.rename(os.path.join(d, runwrap.SPEC), os.path.join(d, runwrap.CANCELLED))
        except OSError:
            return None
        cancelled = os.path.join(d, runwrap.CANCELLED)
        runwrap.forget_command(cancelled, runwrap.read_json(cancelled) or {})
        if error:
            with open(os.path.join(d, "said"), "w", encoding="utf-8") as f:
                f.write(error)
        return "core.run_ended", {"code": None, "reason": reason, **({"error": error} if error else {}),
                                  **({"by": by} if by else {})}

    def _said(self, d):
        try:
            with open(os.path.join(d, "said"), encoding="utf-8") as f:
                return f.read()[:300]
        except OSError:
            return None

    # ---- the end ----------------------------------------------------------------------------------------------------
    def ended(self, run, d):
        res = runwrap.read_json(os.path.join(d, runwrap.RESULT)) or {}
        reason = res.get("reason") if res.get("reason") in ("exit", "timeout", "stopped", "not_started") else "lost"
        data = {"code": res.get("code") if isinstance(res.get("code"), int) else None, "reason": reason,
                "tail": tail(os.path.join(d, runwrap.LOG)), "log": os.path.join(d, runwrap.LOG)}
        if res.get("by"):
            data["by"] = run.get("stop_by") if res["by"] == "signal" and run.get("stop_by") else res["by"]
        if res.get("error"):
            data["error"] = str(res["error"])[:300]
        took = [res.get(k) for k in ("started_at", "ended_at")]
        if all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in took) and took[1] >= took[0]:
            data["took"] = round(took[1] - took[0], 1)   # the wrapper's own: the core may have been down at the end
        return "core.run_ended", data

    def clean(self, run, d, hard):
        """The wrapper is gone without an outcome (hard: it does not answer the stop): stop what is left, check, and
        only then the fact."""
        pids = runwrap.read_json(os.path.join(d, runwrap.PIDS)) or {}
        if hard and runwrap.alive(d) and isinstance(pids.get("pid"), int):
            self._kill_wrapper(pids["pid"], self.launched(d, run))
        leftover = "none"
        mode = self.launched(d, run)
        if mode.get("how") == "unit" and mode.get("unit"):
            unit = mode["unit"]
            self.run(["systemctl", "--user", "stop", unit])
            r = self.run(["systemctl", "--user", "is-active", unit])
            leftover = "unknown" if (r.stdout or "").strip() in ("active", "deactivating") else "none"
        elif mode.get("how") == "session" and isinstance(run.get("pgid"), int):
            leftover = self._stop_group(run)
        if os.path.exists(os.path.join(d, runwrap.RESULT)):
            return self.ended(run, d)[1]
        reason = "stopped" if run["state"] == "stopping" else "lost"
        return {"code": None, "reason": reason, "leftover": leftover, "log": os.path.join(d, runwrap.LOG),
                **({"by": run.get("stop_by")} if reason == "stopped" and run.get("stop_by") else {})}

    def _kill_wrapper(self, pid, mode):
        if mode.get("how") == "unit":
            return   # the unit's stop takes it with the rest
        # the lock was held a moment ago: the number is still the wrapper's (Windows: its job takes the command)
        self.platform.kill_now(pid)

    def _stop_group(self, run):
        """POSIX without systemd: the command's group, only if it is still the one the run started (its leader's start
        time and, on Linux, the same boot) -> "none" (gone) or "unknown" (left alone: the number may be someone's)."""
        pgid, born, p = run["pgid"], run.get("born"), self.platform
        if run.get("boot") and run["boot"] != p.boot_id():
            return "none"   # another boot: nothing of the run survived it
        if born is not None and p.process_start(pgid) == born:
            p.stop_detached(pgid, born)
            for _ in range(GRACE * 5):
                if not p.group_alive(pgid):
                    return "none"
                time.sleep(0.2)
            p.stop_group(pgid, hard=True)
            return "none" if not p.group_alive(pgid) else "unknown"
        return "unknown" if p.group_alive(pgid) else "none"

    # ---- cleaning up ------------------------------------------------------------------------------------------------
    def sweep(self, st, now):
        """Folders of runs that ended KEEP_DAYS ago, the oldest ended ones over ENDED_BYTES, and folders with no
        record that are older than KEEP_DAYS (a rollback, a run pruned from the state)."""
        self.swept = now
        root = os.path.join(self.state_dir, FOLDER)
        try:
            names = os.listdir(root)
        except OSError:
            return
        runs = st.get("hub_runs") or {}
        ended = []
        for n in names:
            path, run = os.path.join(root, n), runs.get(n)
            if run is not None and run["state"] != "ended":
                continue
            if runwrap.alive(path):
                continue   # a wrapper still holds it (a record lost to a rollback): its deadline ends it
            since = (run or {}).get("ended_at") or _mtime(path)
            if now - since >= KEEP_DAYS * 86400:
                self.platform.rmtree(path)
            elif run is not None:
                ended.append((since, path, _size(path)))
        total = sum(s for _, _, s in ended)
        for _, path, size in sorted(ended):
            if total <= ENDED_BYTES:
                break
            self.platform.rmtree(path)
            total -= size


def stop_all(state_dir, wait=HARD_AFTER, platform=None):
    """`hub uninstall`: every live run of this hub is asked to stop and, past `wait`, stopped hard (its unit, its
    wrapper; the wrapper's job or the unit takes the command). By the folders alone: hubd is already down."""
    platform = platform or platforms.current
    root = os.path.join(state_dir, FOLDER)
    live = [os.path.join(root, n) for n in (os.listdir(root) if os.path.isdir(root) else [])
            if runwrap.alive(os.path.join(root, n))]
    for d in live:
        runwrap.write_json(os.path.join(d, runwrap.STOP), {"by": "uninstall"})
    end = time.monotonic() + wait
    while live and time.monotonic() < end:
        live = [d for d in live if runwrap.alive(d)]
        time.sleep(0.2)
    for d in live:
        unit = f"hub-run-{space(state_dir)}-{os.path.basename(d)}"
        spec = runwrap.read_json(os.path.join(d, runwrap.TAKEN)) or {}
        if spec.get("how") == "unit" and shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "stop", spec.get("unit") or unit], capture_output=True, timeout=60)
        pids = runwrap.read_json(os.path.join(d, runwrap.PIDS)) or {}
        if runwrap.alive(d) and isinstance(pids.get("pid"), int):
            pgid = pids.get("pgid")
            # the group only while its leader is this wrapper's child: a number checked against the live wrapper
            ours = isinstance(pgid, int) and platform.parent_of(pgid) == pids["pid"]
            for stop in (platform.ask_stop, platform.kill_now):   # asked first: the wrapper stops its group itself
                stop(pids["pid"])
                for _ in range(50):
                    if not runwrap.alive(d):
                        break
                    time.sleep(0.2)
                if not runwrap.alive(d):
                    break
            if ours and platform.group_alive(pgid):
                platform.stop_group(pgid, hard=True)
    return len(live)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _size(path):
    total = 0
    for base, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total
