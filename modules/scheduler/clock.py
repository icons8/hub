"""Schedule clock — one process for the whole machine instead of launchd/cron per job.

The core decides what to do (the SCHEDULE automaton); the clock only reports facts: "the job's slot has come" (`schedule.due`) and
"the recurring job's term has ended" (`schedule.ended`). Until hubd is up (no fresh `hubd.alive` pulse), the clock waits and
sends nothing: after downtime a recurring job fires once for the last slot (older than 6 h — skipped), a one-shot one —
marked as late. Its own pulse and the nearest term go to `scheduler.json`: the page shows it.
"""
import json
import os
import subprocess
import sys
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
import platforms  # noqa: E402
from core import catalog, lock, perms, profiles, schedule, spool  # noqa: E402
from core.daemon import ALIVE, default_state_dir  # noqa: E402
from core.tail import Tail  # noqa: E402

HUBD_FRESH = 10  # seconds: a hubd pulse older than this means hubd is down, we wait
MAX_SLEEP = 30
MAX_RUN = 15 * 60  # seconds: a job command running longer is stopped
START_WAIT = 60  # seconds: the core did not record schedule.started (an old hubd does not know it) — launch as before
CHECK_TIMEOUT = 120  # seconds: a session job's pre-check running longer is stopped, the session gets a check error
CHECK_ERR = 4000  # characters of the tail of the output of a pre-check that ended with an error
MARK_KEEP = 2 * 86400  # seconds: a launch mark older than this is no longer needed (a slot the core never recorded is skipped)
STATUS = "scheduler.json"


class Clock:
    def __init__(self, state_dir, clock=time.time, awake=time.monotonic, config=None):
        self.state_dir = state_dir
        self.clock = clock
        # hub.json at each launch (`schedule.shell`); an unreadable file keeps the settings read before, it stops no job
        self.config = config or (lambda: catalog.config_or_last(state_dir))
        # the terms of a running job are measured on the awake clock: on macOS and Linux it does not advance while the
        # machine sleeps (the job is frozen with it), as in core/daemon.py check_slept; on Windows it counts sleep, as before
        self.awake = awake
        self.spool = os.path.join(state_dir, "spool")
        self.tail = Tail(os.path.join(state_dir, "journal.jsonl"), page=False)   # only the core state, no page snapshot
        self.sent = {}  # (id, slot or "end") -> when it was sent: one fact per slot until the core records it
        self.limits = {}  # job id -> the seconds its running pre-check may take, as it was when it began
        self.running = {}  # job id -> (process, slot, start on the awake clock, pre-check output file or None for a command, check)
        self.starting = {}  # job id -> (slot, when): schedule.started was sent by this life of the clock — launch as it gets recorded
        self.told = {}  # job id -> slot whose "outcome unknown" this life has reported: not repeated while the core does not record it
        self.logs = os.path.join(state_dir, "logs", "schedule")
        for n in os.listdir(self.logs) if os.path.isdir(self.logs) else []:
            if n.endswith(".check"):   # output of pre-checks from the previous life of the clock: their result will not arrive, they will run again
                try:
                    os.unlink(os.path.join(self.logs, n))
                except OSError:
                    pass
        self.swept = None
        self.sweep(self.clock())

    def sweep(self, now):
        """Launch marks (`<id>.started`, and the `.tmp` of an interrupted write) of old runs and removed jobs."""
        self.swept = now
        for n in os.listdir(self.logs) if os.path.isdir(self.logs) else []:
            if n.endswith((".started", ".started.tmp")):
                p = os.path.join(self.logs, n)
                try:
                    if now - os.path.getmtime(p) > MARK_KEEP:
                        os.unlink(p)
                except OSError:
                    pass

    def hubd_up(self):
        try:
            return self.clock() - os.path.getmtime(os.path.join(self.state_dir, ALIVE)) < HUBD_FRESH
        except OSError:
            return False

    def step(self):
        """One turn: how long to sleep until the next one."""
        now = self.clock()
        if now - self.swept >= 3600:
            self.sweep(now)
        self.tail.poll()
        st = self.tail.reader.state
        self.finish(now, st)   # and while hubd is down: the pre-check timeout holds, the result goes to the mailbox
        if not self.hubd_up():
            self.status("waiting", None)
            return 2
        tz = st["owner"]["tz"]
        soonest = None
        self.sent = {k: t for k, t in self.sent.items() if now - t < 600}
        # the Mac between sleeps (core.dark, reducer _dark): a command or a pre-check would hang on the silent network
        # until the next windows — they wait for the full wake; a slot still comes, its message waits in the queue
        dark = "dark" in st
        for job in list(st.get("schedule", {}).values()):
            if job.get("command") and job.get("run_slot") and job.get("ran_slot") != job["run_slot"] \
                    and job["id"] not in self.running and (job["id"], "ran", job["run_slot"]) not in self.sent \
                    and not dark:
                self.launch(job, now)  # the core decided "launch" — execute, no more than once
            if job["state"] != "active":
                continue
            slot = schedule.slot_due(job, now, tz)
            if slot is not None and job.get("check"):
                # pre-check — before the slot: its code and output go in the same schedule.due; one per job at a time
                if job["id"] not in self.running and (job["id"], slot) not in self.sent and not dark:
                    self.check(job, slot, now, st)
            elif slot is not None:
                self.put("schedule.due", job["id"], {"slot": slot}, (job["id"], slot), now)
            elif job["kind"] != "at" and now > schedule.end(job):
                self.put("schedule.ended", job["id"], {}, (job["id"], "end"), now)
            due = schedule.next_due(job, now, tz)
            if due is not None and (soonest is None or due < soonest):
                soonest = due
        self.status("running", soonest)
        if any(r[3] for r in self.running.values()):
            return 1   # a pre-check is running: its result (the session is woken by it) and timeout — not in half a minute
        return max(1, min(MAX_SLEEP, (soonest or now + MAX_SLEEP) - now))

    def env(self, job):
        os.makedirs(self.logs, exist_ok=True)
        # user_env (login, network and the names of hub.json `user_env`) as the sessions have it: a command or a check
        # of the schedule is the same work and called the owner's tools without their credentials
        env = {**platforms.current.owner_env(os.environ), **catalog.user_env(), "HUB_SCHEDULE_JOB": job["id"],
               "HUB_STATE_DIR": self.state_dir,
               "PATH": os.path.join(REPO, "bin") + os.pathsep + os.environ.get("PATH", "")}
        for k in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE"):
            env.pop(k, None)
        # a command speaks as the one who wrote it (core/schedule.speaker_of): a session's — with its HUB_SESSION, so
        # its `hub` commands pass that session's gates, not the owner's terminal; the owner's and the core's — as before
        kind, name = schedule.speaker_of(job) if job.get("command") else ("owner", None)
        if kind == "session":
            env["HUB_SESSION"] = name
        elif kind == "plugin" and name.startswith("mod:"):
            env["HUB_MODULE"] = name[4:]
        elif kind == "plugin":
            env["HUB_EXTENSION"] = name[4:] if name.startswith("ext:") else name
        # on the account gateway like a session (core/profiles.job_env): its `claude -p` would otherwise go to the machine's login
        return profiles.job_env(env, self.state_dir, self.tail.reader.state)

    def shell(self, command, env):
        """What to start for a job's shell string -> (Popen args, shell=, env): /bin/sh or cmd.exe, on Windows Git Bash
        with hub.json `schedule.shell` "bash" (read at each launch)."""
        return platforms.current.job_shell(command, env, self.state_dir, schedule.shell(self.config()))

    def check(self, job, slot, now, st):
        """A session job's pre-check: on behalf of that session and in its folder (`hub` commands in it work as from the session)."""
        env = {**self.env(job), "HUB_SESSION": job["session"]}
        cwd = (st["sessions"].get(job["session"]) or {}).get("cwd") or ""
        # its own file per launch: a check that survived a clock restart does not write into the output of a new one
        out = os.path.join(self.logs, f"{job['id']}.{uuid.uuid4().hex[:8]}.check")
        args, shell, env = self.shell(job["check"], env)
        with open(out, "w") as log:
            proc = subprocess.Popen(args, shell=shell, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True, umask=perms.user_umask(),
                                    cwd=cwd if os.path.isdir(cwd) else os.path.expanduser("~"))
        self.running[job["id"]] = (proc, slot, self.awake(), out, job["check"])
        self.limits[job["id"]] = self.check_limit(st, job["id"])

    def launch(self, job, now):
        """"Started" — as a fact in the core before the launch: a clock restart in the middle of a command (or after it ends, before the result is recorded)
        does not launch it a second time — a double external effect (two backup uploads). A launch recorded by a previous life of the
        clock, without a result — "outcome unknown" (a pid is not enough: a process that exited before the result was recorded is not visible)."""
        jid, slot = job["id"], job["run_slot"]
        mine = self.starting.get(jid)
        mine = mine if mine and mine[0] == slot else None
        if not mine and (job.get("started_slot") == slot or self.launched(jid) == slot):
            # recorded by the core, or marked on this machine before Popen (the core lags longer than START_WAIT, or the
            # result expired from `sent`): run in a previous life of the clock — "outcome unknown". Told once: the core
            # that does not record it (stands, lags) would get the same report every 10 minutes, all but the first refused
            if self.told.get(jid) != slot:
                self.told[jid] = slot
                self.put("schedule.ran", jid, {"slot": slot, "exit": "unknown"}, (jid, "ran", slot), now)
        elif job.get("started_slot") == slot or mine and now - mine[1] >= START_WAIT:
            self.starting.pop(jid, None)
            self.start(job, now)
        else:
            self.starting[jid] = mine or (slot, now)
            self.put("schedule.started", jid, {"slot": slot}, (jid, "start", slot), now)

    def mark(self, jid):
        return os.path.join(self.logs, f"{jid}.started")

    def launched(self, jid):
        """The slot this clock (in any of its lives) last started the job's command for, or None."""
        try:
            with open(self.mark(jid), encoding="utf-8") as f:
                return json.load(f).get("slot")
        except (OSError, ValueError, AttributeError):
            return None

    def start(self, job, now):
        """A command job: the shell string of whoever wrote it (env); what to tell the owner is decided by the command
        itself (`hub notify`)."""
        env = self.env(job)
        tmp = self.mark(job["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:   # before Popen: a restart right after the launch must see it
            json.dump({"slot": job["run_slot"], "at": now}, f)
        platforms.current.replace(tmp, self.mark(job["id"]))
        with open(os.path.join(self.logs, f"{job['id']}.log"), "a") as log:
            log.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            log.flush()
            # the user's command: its files get the user's umask, not the hub's closed one (core/perms.py)
            args, shell, env = self.shell(job["command"], env)
            proc = subprocess.Popen(args, shell=shell, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, cwd=os.path.expanduser("~"), start_new_session=True,
                                    umask=perms.user_umask())
        self.running[job["id"]] = (proc, job["run_slot"], self.awake(), None, None)

    @staticmethod
    def check_limit(st, jid):
        """How long job jid's pre-check may run: its own `check_timeout` (the core validated it), otherwise CHECK_TIMEOUT."""
        own = ((st.get("schedule") or {}).get(jid) or {}).get("check_timeout")
        return own if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0 else CHECK_TIMEOUT

    def finish(self, now, st):
        awake = self.awake()   # once a pass, as `now`: every running job is judged at the same moment
        for jid, (proc, slot, began, out, check) in list(self.running.items()):
            code = proc.poll()
            work = awake - began
            if code is None and work > (MAX_RUN if out is None else self.limits.get(jid, CHECK_TIMEOUT)):
                # the job and check shell is in its own session: the whole group at once (SIGKILL), otherwise what it launched lives
                # past the deadline and keeps the output open, and a descendant deaf to SIGTERM lives on. Not yet reaped (poll()
                # gave None) — the number is still this child's; on Windows kill_detached stops the tree
                try:
                    platforms.current.kill_detached(proc, hard=True)
                except OSError:
                    pass   # it had already exited by itself
                proc.kill()
                proc.wait()
                code = "timeout"
            if code is None:
                continue
            del self.running[jid]
            self.limits.pop(jid, None)
            data = {"slot": slot, "exit": code, "took": round(work, 1)}
            if out is None:
                self.put("schedule.ran", jid, data, (jid, "ran", slot), now)
                continue
            if code != 0:   # "nothing new" — without output: the journal does not collect quiet ticks
                data["out"] = self.output(jid, slot, out, code)
            try:
                os.unlink(out)
            except OSError:
                pass
            if ((st.get("schedule") or {}).get(jid) or {}).get("check") != check:
                continue   # the check was changed or removed while it ran: the result is not about this job — the next turn will decide by the slot
            self.put("schedule.due", jid, data, (jid, slot), now)

    def output(self, jid, slot, path, code):
        """The full pre-check output goes into the job text; longer than CHECK_OUT — the beginning and where the full one is.
        A check error — only the tail: a broken check every minute does not bloat the journal."""
        try:
            with open(path, "rb") as f:
                if code != schedule.CHECK_NEW:
                    f.seek(max(0, os.path.getsize(path) - CHECK_ERR * 4))
                raw = f.read(schedule.CHECK_OUT * 4 + 1)
        except OSError:
            return ""
        text = raw.decode("utf-8", "replace")
        if code != schedule.CHECK_NEW:
            return text[-CHECK_ERR:]
        if len(text) <= schedule.CHECK_OUT and len(raw) <= schedule.CHECK_OUT * 4:
            return text
        for n in os.listdir(self.logs):   # full outputs from past days — remove
            p = os.path.join(self.logs, n)
            if n.startswith(f"{jid}.") and n.endswith(".full.txt") and time.time() - os.path.getmtime(p) > 86400:
                os.unlink(p)
        full = os.path.join(self.logs, f"{jid}.{int(slot)}.full.txt")
        try:
            platforms.current.replace(path, full)
        except OSError:
            full = "not saved"
        return text[:schedule.CHECK_OUT] + f"\n… [output longer than {schedule.CHECK_OUT} characters — in full: {full}]"

    def put(self, type, jid, data, key, now):
        if key not in self.sent:
            spool.put(self.spool, type, jid, data, src="scheduler")
            self.sent[key] = now

    def status(self, state, soonest):
        path = os.path.join(self.state_dir, STATUS)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "ts": self.clock(), "state": state, "next": soonest}, f)
        platforms.current.replace(tmp, path)   # Windows: cannot replace while the page reads the file — wait for it


def read_status(state_dir, now=None):
    """For the page: whether the clock works and whether it waits for hubd. A pulse older than a minute — the clock does not work."""
    try:
        with open(os.path.join(state_dir, STATUS), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {"state": "down", "next": None}
    if (now or time.time()) - st.get("ts", 0) > 2 * MAX_SLEEP + 5:
        return {"state": "down", "next": None}
    return {"state": st.get("state"), "next": st.get("next")}


def main():
    state_dir = default_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    held = lock.acquire(os.path.join(state_dir, "scheduler.lock"))
    if held is None:
        sys.exit("the schedule clock is already running: one process per machine")
    clock = Clock(state_dir)
    while True:
        end = time.time() + clock.step()
        up = clock.hubd_up()
        while time.time() < end:
            time.sleep(2)
            if clock.tail.poll() or clock.hubd_up() != up:
                break  # new in the journal (a job was added, a slot recorded) or hubd went down/up — recompute at once


if __name__ == "__main__":
    main()
