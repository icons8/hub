"""The page's "Add a hub" and "Remove" (Settings -> Main -> Hubs on this computer, modules/web/instancesweb.py) as a
process of its own: `hub instance new|uninstall` takes a minute (a clone, the installer, the new page's first answer)
and removing this very hub stops its service — the page's server with it. So the page writes the job into
<state>/instance-job.json and starts this file apart from itself (on Linux under systemd — a unit of its own, so the
service's stop does not take it along); it runs core/instancecli.py with its words, and the installer's, in
<state>/logs/instance-job.log, then writes the exit code into the job. The page reads the job and the log's last lines.

Usage: python3 core/instancejob.py <state folder> <job id>"""
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB = "instance-job.json"
LOG = os.path.join("logs", "instance-job.log")
OPS = {"new": "new", "remove": "uninstall"}


def path(state):
    return os.path.join(state, JOB)


def read(state):
    """The job as written, or None (no file, a broken one)."""
    try:
        with open(path(state), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(state, data):
    """Whole and for the owner only (0600): a temp file next to it, then a replace."""
    fd, tmp = tempfile.mkstemp(dir=state, prefix=JOB + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path(state))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def argv_of(job):
    """The instancecli arguments of a job: new <name> [--engine codex] [--claude own] --yes; uninstall <name> --yes."""
    name = job["name"]
    if job["op"] == "new":
        codex = job.get("engine") == "codex"   # the Claude sign-in is not a Codex hub's choice: `--claude` refused
        extra = (["--engine", "codex"] if codex else []) + (["--claude", "own"] if not codex and job.get("claude") == "own" else [])
        return ["new", name, *extra, "--yes"]
    return ["uninstall", name, "--yes"]


def run(state, job_id, cli=None):
    """Run the job once: the log, instancecli, the exit code into the job. cli — instancecli.main in tests (then this
    process's own descriptors stay as they are)."""
    job = read(state)
    if not job or job.get("id") != job_id or job.get("op") not in OPS or "ended" in job:
        return 2
    os.makedirs(os.path.join(state, "logs"), exist_ok=True)
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import platforms
    code = 1
    log = open(os.path.join(state, LOG), "w", encoding="utf-8", buffering=1)
    try:
        pid = os.getpid()   # the page asks by it whether this job still runs
        job = read(state) or {}
        if job.get("id") != job_id:   # the page gave up on it (lost) and wrote another one meanwhile
            return 2
        write(state, {**job, "pid": pid, "born": platforms.current.process_start(pid)})
        if cli is None:   # the installer this starts writes to fd 1 and 2: the same log
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            sys.stdout = sys.stderr = log
            if job.get("lang"):
                from core import i18n
                i18n.use(job["lang"])
            from core import instancecli
            cli = instancecli.main
        try:
            code = cli(argv_of(job), say=lambda *a, **k: print(*a, **k, file=log, flush=True))
        except Exception as e:  # noqa: BLE001 — the page shows why, whatever it was
            print(f"{type(e).__name__}: {e}", file=log, flush=True)
            code = 1
    finally:
        log.close()
        now = read(state)
        if now and now.get("id") == job_id:   # not cleared by the page, not another job's file
            try:
                write(state, {**now, "ended": int(time.time()), "code": code})
            except OSError:
                pass   # removed with its hub (an uninstall of this very hub with --purge)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip().splitlines()[-1])
    sys.exit(run(sys.argv[1], sys.argv[2]))
