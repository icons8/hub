#!/usr/bin/env python3
"""The tests of one commit (a branch, a colleague's PR) on the other systems before it is merged, not only before a release:
Windows as a standard user — the way the update self-check runs on the machine of a person who started their hub — and the
Mac bridge; one table of reds with their reasons per machine, tied to the commit (a check by the publisher, not needed by
every hub).

    tools/pr_stands.py [<ref>] [--only tests.x …] [--no-windows] [--no-mac] [--out DIR] [--timeout S]
    tools/pr_stands.py --verdict <ref> [--out DIR]

Windows — HUB_STAND_WIN (user@host over ssh), its folder HUB_STAND_WIN_DIR (default C:\\hubrev\\pr-stands) and Python
HUB_STAND_WIN_PYTHON (the py launcher's version, default 3.14). The machine sleeps until needed: HUB_STAND_WIN_WAKE wakes
it, HUB_STAND_WIN_SLEEP puts it back to sleep — only when this run woke it. Several sessions share it: HUB_STAND_WIN_LOCK
is a lock file held from waking to sleeping (flock(2), the same as the `flock` command). The commit goes there as a git
bundle; `tools/test.py --no-lock` runs in a scheduled task in the user's own session (schtasks /it) through
`runas /trustlevel:0x20000` — a standard user's token. The run checks it got one (`fltmc`, which only an elevated token
may run: "access denied" 0x80070005 — the same code in every language of Windows): an administrator's run, or a probe that
failed otherwise, is an error, not a green. A run that did not end
in time is left alone — nothing there is killed (each run has its own folder there, a later one does not step on it); the
machine goes back to sleep if this run woke it.

Mac — HUB_STAND_MAC, a command that takes `--repo <repo> --sha <commit>` and answers with its exit code: 0 green, 1 red,
anything else — the Mac is unavailable (as the Mac bridge's `test`).

The result goes to <out>/<commit>.json (default <temp>/pr-stands; a machine skipped this time keeps its earlier result there,
a run of a part of the suite goes to <commit>-only.json); --verdict tells whether exactly this commit — not an earlier one of
the branch — went green on every machine it ran on. Exit code: 0 green, 1 red, 2 a stand failed, is not set
or the commit was not run.
"""
import argparse
import contextlib
import datetime
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

OK, RED, ERROR, SKIPPED = "ok", "red", "error", "skipped"
FOLDER = "C:\\hubrev\\pr-stands"
PYTHON = "3.14"
TIMEOUT = 3600   # the whole suite there: 931 s on four cores (03.11), plus the clone and a slow start
SAFE_FOLDER = re.compile(r"[A-Za-z]:(\\[\w.-]+)+")   # goes into cmd lines unquoted-safe: no spaces, & | < > ^ % "
SAFE_USER = re.compile(r"[\w][\w.-]{0,63}")
SAFE_TEST = re.compile(r"tests(\.\w+)*")
RAN = re.compile(r"^Ran (\d+) tests?", re.M)
FAILED = re.compile(r"^(?:FAIL|ERROR): \S+ \(([\w.]+)\)", re.M)   # as in tools/test.py: the full name in parentheses
# what fltmc prints to a token without the right (E_ACCESSDENIED; the words around it follow the language of Windows, the
# code does not; its exit code is 1, as for any other failure): only this is "standard" — any other failure of the probe
# says nothing about the token (fail closed). An elevated token lists the filters, exit code 0 (winstand, 03.11)
DENIED = "0x80070005"
START_WAIT = 600   # the task starts within seconds; no start in 10 min — no user logged on, or runas refused


def tail(text, lines=3, limit=300):
    out = " · ".join(x.strip() for x in str(text).strip().splitlines()[-lines:] if x.strip())
    return out if len(out) <= limit else "…" + out[-limit:]


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {tail(r.stderr)}")
    return r


def sha_of(repo, ref):
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def bundle(repo, sha, path):
    """The commit with its history into a git bundle under refs/pr-stands/<sha>: a bundle needs a ref, and the ref is
    removed at once — the branches of the repo are not touched."""
    ref = f"refs/pr-stands/{sha}"
    git(repo, "update-ref", ref, sha)
    try:
        git(repo, "bundle", "create", path, ref)
    finally:
        git(repo, "update-ref", "-d", ref, check=False)
    return ref


def parse(out):
    """tools/test.py output → {ran, failed: [names], reasons: {name: the last line of its traceback}, verdict: OK | FAILED | None}."""
    ran = [int(m.group(1)) for m in RAN.finditer(out)]
    failed = list(dict.fromkeys(n if n.startswith("tests.") else "tests." + n for n in FAILED.findall(out)))
    reasons = {}
    lines = out.splitlines()
    for i, line in enumerate(lines):
        m = FAILED.match(line)
        if not m:
            continue
        name = m.group(1) if m.group(1).startswith("tests.") else "tests." + m.group(1)
        block, body = [], False
        for nxt in lines[i + 1:]:   # the traceback is between the dashes under the header (and its docstring) and the next rule
            if nxt.startswith(("=" * 20, "-" * 20)):
                if body or nxt.startswith("=" * 20):
                    break
                body = True
                continue
            if body:
                block.append(nxt)
        said = [x.strip() for x in block if x.strip()]
        reasons.setdefault(name, said[-1][:300] if said else "")
    verdict = None
    for line in reversed(lines):
        if line.startswith("OK"):
            verdict = "OK"
            break
        if line.startswith("FAILED"):
            verdict = "FAILED"
            break
    # the runner's own total is the last "Ran N" (the chunks print theirs above it)
    return {"ran": ran[-1] if ran else None, "failed": failed, "reasons": reasons, "verdict": verdict}


def result(status, detail="", **more):
    return {"status": status, "detail": detail, **more}


class Ssh:
    """The other machine over ssh/scp (BatchMode: no password prompts). ssh and scp — a program or its argv."""
    OPTS = ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR"]

    def __init__(self, target, ssh="ssh", scp="scp"):
        self.target = target
        self.ssh, self.scp = ([x] if isinstance(x, str) else list(x) for x in (ssh, scp))

    def run(self, command, timeout=120):
        """(code, what the command printed): ssh's own warnings go to stderr and are not the answer (03.11: a client
        warning about the key exchange was read as the user name); stderr is added only to a failure."""
        try:
            r = subprocess.run([*self.ssh, *self.OPTS, self.target, command], capture_output=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            return 255, str(e)
        out = r.stdout.decode("utf-8", "replace")
        return r.returncode, out if r.returncode == 0 else out + r.stderr.decode("utf-8", "replace")

    def alive(self):
        return self.run("echo ok", timeout=30)[0] == 0

    def _copy(self, src, dst, timeout=900):
        try:
            r = subprocess.run([*self.scp, "-q", *self.OPTS, src, dst], capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        return r.returncode == 0, tail(r.stderr)

    def put(self, local, remote):
        return self._copy(local, f"{self.target}:{scp_path(remote)}")

    def get(self, remote, local):
        return self._copy(f"{self.target}:{scp_path(remote)}", local)


def scp_path(path):
    """C:\\a\\b → /C:/a/b: without the leading slash scp takes "C" for a host and silently copies nothing."""
    return "/" + path.replace("\\", "/") if re.match(r"[A-Za-z]:", path) else path


def take(path, say=print, pause=time.sleep, every=30):
    """The machine's lock file, held open; waits while another run holds it."""
    from core import lock
    said = False
    while True:
        f = lock.acquire(path)
        if f:
            return f
        if not said:
            say(f"the machine is busy with another run (lock {path}) — waiting", flush=True)
            said = True
        pause(every)


def on_machine(remote, work, wake=None, sleep=None, lock=None, wake_wait=600, poll=10, say=print, pause=time.sleep,
               clock=time.monotonic):
    """work() on the other machine under its lock: a sleeping machine is woken with `wake` and put back to sleep with
    `sleep` afterwards; a machine that was already up is left as it was."""
    held = take(lock, say=say, pause=pause) if lock else None
    woke = False
    try:
        if not remote.alive():
            if not wake:
                return result(ERROR, "the machine does not answer over ssh and there is no command to wake it")
            woke = True
            say("waking the machine…", flush=True)
            try:
                r = subprocess.run(wake, shell=True, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as e:
                return result(ERROR, f"could not wake it: {e}")
            if r.returncode != 0:
                return result(ERROR, "could not wake it: " + tail(r.stderr or r.stdout))
            end = clock() + wake_wait
            while not remote.alive():
                if clock() > end:
                    return result(ERROR, "woke it, but ssh did not answer")
                pause(poll)
        return work()
    finally:
        try:
            if woke and sleep:   # a hung sleep command must not swallow the result of the run
                subprocess.run(sleep, shell=True, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001 — a hung or broken sleep command: the result and the lock come first
            say(f"could not put the machine to sleep: {e}", flush=True)
        finally:
            if held:
                held.close()


def scripts(folder, name, ref, python, only):
    """(the task's .cmd, the inner .cmd): the task in the user's session starts the inner one with a standard user's token
    (runas refused — the status says so at once); the inner one records the token (`fltmc` runs only elevated; the groups
    for the reader), builds the copy from the bundle and runs the suite as the update self-check does; the status file
    appears last."""
    at = f"{folder}\\{name}"
    inner = "\r\n".join([
        "@echo off",
        f'cd /d "{folder}"',
        f'whoami /groups > "{at}.groups" 2>&1',
        f'fltmc > "{at}.fltmc" 2>&1',
        # redirection first: "echo x 1> f" would take a one-digit code for a handle (03.11, live: the code was lost)
        f'> "{at}.token" echo fltmc %errorlevel%',
        f'if exist "{at}" rmdir /s /q "{at}"',
        f'git init -q "{at}" > "{at}.log" 2>&1 || goto out',
        f'cd /d "{at}"',
        f'git fetch -q "{at}.bundle" {ref} >> "{at}.log" 2>&1 || goto out',
        f'git checkout -q -B main FETCH_HEAD >> "{at}.log" 2>&1 || goto out',
        'set "PYTHONUTF8=1"',
        f'py -{python} tools\\test.py --no-lock {" ".join(only)} >> "{at}.log" 2>&1',
        ":out",
        f'echo done %errorlevel% > "{at}.status"',
        ""])
    task = "\r\n".join(["@echo off", f'runas /trustlevel:0x20000 "{at}.inner.cmd"',
                         f'if errorlevel 1 echo done runas %errorlevel% > "{at}.status"', ""])
    return task, inner


def windows(remote, repo, sha, folder=FOLDER, python=PYTHON, only=(), timeout=TIMEOUT, poll=30, say=print,
            pause=time.sleep, clock=time.monotonic):
    """The suite of the commit on the Windows machine as a standard user → result with ran, failed, reasons, seconds."""
    if not SAFE_FOLDER.fullmatch(folder):
        return result(ERROR, f"folder on the machine {folder!r}: only letters, digits, . - _ (it goes into cmd lines)")
    if not re.fullmatch(r"3\.\d+", python):
        return result(ERROR, f"Python version {python!r}: of the form 3.14")
    if any(not SAFE_TEST.fullmatch(n) for n in only):
        return result(ERROR, "test names — of the form tests.module.Class.test")
    code, said = remote.run("echo %USERNAME%")
    user = said.strip().splitlines()[-1].strip() if said.strip() else ""
    if code or not SAFE_USER.fullmatch(user):
        return result(ERROR, f"could not find out the user on the machine: {tail(said)}")
    # its own name per run: a run that timed out may still go on there, and a later run of the same commit must not delete
    # its copy or read its status
    name = f"ps-{sha[:12]}-{secrets.token_hex(3)}"
    at = f"{folder}\\{name}"
    t0 = clock()
    with tempfile.TemporaryDirectory(prefix="pr-stands-") as tmp:
        ref = bundle(repo, sha, os.path.join(tmp, "x.bundle"))
        task, inner = scripts(folder, name, ref, python, only)
        for local, text in (("task.cmd", task), ("inner.cmd", inner)):
            with open(os.path.join(tmp, local), "w", newline="") as f:
                f.write(text)
        code, said = remote.run(f'(if not exist "{folder}" mkdir "{folder}") & del "{at}.status" 2>nul & '
                                f'if exist "{at}.status" exit 1')
        if code:
            return result(ERROR, f"folder on the machine: {tail(said)}")
        for local, there in (("x.bundle", f"{at}.bundle"), ("task.cmd", f"{at}.cmd"), ("inner.cmd", f"{at}.inner.cmd")):
            ok, why = remote.put(os.path.join(tmp, local), there)
            if not ok:
                return result(ERROR, f"scp {local}: {why}")
        code, said = remote.run(f'schtasks /create /tn {name} /tr "{at}.cmd" /sc once /st 23:59 /it /ru {user} /f '
                                f'&& schtasks /run /tn {name}')
        if code:
            return result(ERROR, f"scheduler task: {tail(said)}")
        say(f"Windows: run of {sha[:9]} in progress ({name})", flush=True)
        end, start_by = clock() + timeout, clock() + START_WAIT
        status, started = "", False
        while True:
            code, said = remote.run(f'type "{at}.status"')
            if code == 0 and said.strip().startswith("done"):
                status = said.strip()
                break
            if not started and remote.run(f'type "{at}.token"')[0] == 0:
                # started: the task goes at once, so its one-time trigger (/sc once) can never start a second copy
                started = True
                remote.run(f"schtasks /delete /tn {name} /f")
            if not started and clock() > start_by:
                remote.run(f"schtasks /delete /tn {name} /f")
                if remote.run(f'type "{at}.token"')[0] == 0:   # it started just now, before the task went: wait for it
                    started = True
                    continue
                # nothing runs there: what was uploaded goes (each run has its own name, no later run removes it)
                remote.run(f'del "{at}.bundle" "{at}.cmd" "{at}.inner.cmd" "{at}.status"')
                return result(ERROR, f"the scheduler task did not start within {START_WAIT} s (user {user} not logged "
                              "in to a session?)", seconds=round(clock() - t0))
            if clock() > end:
                remote.run(f"schtasks /delete /tn {name} /f")
                return result(ERROR, f"the run did not finish within {timeout} s — nothing was killed on the machine, the copy {at} is left",
                              seconds=round(clock() - t0))
            pause(poll)
        log = os.path.join(tmp, "run.log")
        ok, why = remote.get(f"{at}.log", log)
        out = ""
        if ok:
            with open(log, encoding="utf-8", errors="replace") as f:
                out = f.read()
        _, token = remote.run(f'type "{at}.token"')
        _, said_fltmc = remote.run(f'type "{at}.fltmc"')
        _, groups = remote.run(f'type "{at}.groups"')
        remote.run(f'schtasks /delete /tn {name} /f & rmdir /s /q "{at}" & del "{at}.bundle" "{at}.cmd" "{at}.inner.cmd" '
                   f'"{at}.token" "{at}.fltmc" "{at}.groups" "{at}.status" "{at}.log"')
    seconds = round(clock() - t0)
    if status.startswith("done runas"):
        return result(ERROR, f"runas did not give a standard user's token ({status})", seconds=seconds, output=out)
    if not ok:
        return result(ERROR, f"could not fetch the run log: {why}", seconds=seconds)
    probe = token.split()
    if len(probe) != 2 or probe[0] != "fltmc" or probe[1] == "0" or DENIED not in said_fltmc.lower():
        admins = [x.strip() for x in groups.splitlines() if "S-1-5-32-544" in x]
        return result(ERROR, f"the run did not go with a standard user's token ({token.strip() or 'token not recorded'}): "
                      + tail(" ".join(admins) or groups), seconds=seconds, output=out)
    got = parse(out)
    if got["ran"] is None or got["verdict"] is None:
        return result(ERROR, f"the tests did not start ({status}): {tail(out)}", seconds=seconds, output=out)
    return result(OK if got["verdict"] == "OK" and not got["failed"] else RED, tail(out, 2), seconds=seconds, output=out,
                  **got)


def mac(command, repo, sha, timeout=TIMEOUT):
    """The suite of the commit on the Mac through the bridge command (`… --repo R --sha S`; 0 green, 1 red, else unavailable)."""
    t0 = time.monotonic()
    try:
        r = subprocess.run([*shlex.split(command), "--repo", repo, "--sha", sha], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:   # what it printed until then names the test that hung
        said = "".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else x for x in (e.stdout, e.stderr) if x)
        return result(ERROR, f"the Mac did not answer within {timeout} s", seconds=round(time.monotonic() - t0), output=said)
    except OSError as e:
        return result(ERROR, f"Mac command: {e}")
    out = r.stdout + r.stderr
    seconds = round(time.monotonic() - t0)
    if r.returncode not in (0, 1):
        return result(ERROR, f"the Mac is unavailable (code {r.returncode}): {tail(out)}", seconds=seconds, output=out)
    got = parse(out)
    return result(OK if r.returncode == 0 else RED, tail(out, 2), seconds=seconds, output=out, **got)


def table(machines):
    lines = []
    for m, r in machines.items():
        head = {OK: "green", RED: "red", ERROR: "stand failure", SKIPPED: "not set"}.get(r["status"], r["status"])
        ran = f", tests {r['ran']}" if r.get("ran") is not None else ""
        sec = f", {r['seconds']} s" if r.get("seconds") is not None else ""
        lines.append(f"{m}: {head}{ran}{sec}" + (f" — {r['detail']}" if r["status"] in (ERROR, SKIPPED) else ""))
        for n in r.get("failed") or []:
            lines.append(f"  {n} — {(r.get('reasons') or {}).get(n, '')}")
    return "\n".join(lines)


def code_of(machines):
    statuses = [r["status"] for r in machines.values()]
    ran = [s for s in statuses if s != SKIPPED]
    if not ran or ERROR in ran:
        return 2
    return 1 if RED in ran else 0


def out_dir(given):
    return given or os.path.join(tempfile.gettempdir(), "pr-stands")


def verdict(folder, sha):
    """(exit code, text) for exactly this commit from the saved result."""
    try:
        with open(os.path.join(folder, f"{sha}.json"), encoding="utf-8") as f:
            saved = json.load(f)
        machines = saved["machines"]
    except (OSError, ValueError, KeyError, TypeError):
        return 2, f"commit {sha[:9]} has not run on the stands (no {os.path.join(folder, sha + '.json')})"
    text = f"commit {sha[:9]} ({saved.get('when', '?')}):\n{table(machines)}"
    if saved.get("only"):   # a part of the suite says nothing about the rest
        return 2, text + f"\nonly a part of the suite ran ({' '.join(saved['only'])}) — this is not the commit's verdict"
    return code_of(machines), text


def record(folder, sha, ref, only, machines):
    """The result into <sha>.json — the whole suite; a machine this run skipped keeps its earlier result there (a run of
    the Mac alone does not forget a red Windows). A part of the suite (--only) goes to <sha>-only.json: it is no verdict.
    The whole output of each machine that ran — next to it, <sha>[-only].<machine>.log (the table keeps one line a red);
    one that ran without output has none (not the log of an earlier run)."""
    from core import lock
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{sha}-only.json" if only else f"{sha}.json")
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    outputs = {m: r.get("output") for m, r in machines.items() if r["status"] != SKIPPED}
    machines = {m: {k: v for k, v in r.items() if k != "output"} for m, r in machines.items()}
    with lock.exclusive(path + ".lock"):   # two runs of one commit (Windows here, the Mac there) do not lose each other
        for m, out in outputs.items():   # under the lock: the log and the verdict next to it are of one run
            log = f"{path[:-len('.json')]}.{m}.log"
            if not out:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(log)
                continue
            with open(f"{log}.{os.getpid()}", "w", encoding="utf-8") as f:
                f.write(out)
            os.replace(f"{log}.{os.getpid()}", log)
        merged = {}
        if not only:
            try:
                with open(path, encoding="utf-8") as f:
                    merged = dict(json.load(f)["machines"])
            except (OSError, ValueError, KeyError, TypeError):
                merged = {}
        for m, r in machines.items():
            if r["status"] != SKIPPED or m not in merged:
                merged[m] = {**r, "when": now}
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sha": sha, "ref": ref, "when": now, "only": only, "machines": merged}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)   # a reader (--verdict) sees the old file or the new one, never half of it
    return path, merged


def main(argv=None, environ=None, remote=Ssh, say=print):
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ref", nargs="?", default="HEAD")
    ap.add_argument("--repo", default=REPO, help="the repository with the commit (default: this one)")
    ap.add_argument("--only", nargs="+", default=[], metavar="tests.x", help="only these tests (default: the whole suite)")
    ap.add_argument("--no-windows", action="store_true")
    ap.add_argument("--no-mac", action="store_true")
    ap.add_argument("--out", help="folder of the results (<commit>.json)")
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    ap.add_argument("--verdict", action="store_true", help="only state the verdict of an already run commit")
    a = ap.parse_args(argv)
    try:
        sha = sha_of(a.repo, a.ref)
    except RuntimeError as e:
        say(str(e))
        return 2
    folder = out_dir(a.out)
    if a.verdict:
        code, text = verdict(folder, sha)
        say(text)
        return code
    machines, jobs = {}, []
    win = "" if a.no_windows else environ.get("HUB_STAND_WIN", "")
    macs = "" if a.no_mac else environ.get("HUB_STAND_MAC", "")
    if win:
        def run_windows():
            box = remote(win)
            machines["windows"] = on_machine(
                box, lambda: windows(box, a.repo, sha, environ.get("HUB_STAND_WIN_DIR") or FOLDER,
                                     environ.get("HUB_STAND_WIN_PYTHON") or PYTHON, a.only, a.timeout, say=say),
                wake=environ.get("HUB_STAND_WIN_WAKE"), sleep=environ.get("HUB_STAND_WIN_SLEEP"),
                lock=environ.get("HUB_STAND_WIN_LOCK"), say=say)
        jobs.append(run_windows)
    elif not a.no_windows:
        machines["windows"] = result(SKIPPED, "HUB_STAND_WIN is not set")
    if macs:
        jobs.append(lambda: machines.update(mac=mac(macs, a.repo, sha, a.timeout)))
    elif not a.no_mac:
        machines["mac"] = result(SKIPPED, "HUB_STAND_MAC is not set")
    threads = [threading.Thread(target=j) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for m in ("windows", "mac"):   # a stand that crashed inside its thread is a failure, not silence
        if (m == "windows" and win or m == "mac" and macs) and m not in machines:
            machines[m] = result(ERROR, "the stand crashed (see the output above)")
    path, merged = record(folder, sha, a.ref, a.only, machines)
    # what is printed and the exit code are the commit's (with a machine kept from an earlier run), as --verdict says it
    say(f"commit {sha[:9]}{' (only ' + ' '.join(a.only) + ')' if a.only else ''}:\n{table(merged)}\nresult: {path}")
    return code_of(merged)


if __name__ == "__main__":
    sys.exit(main())
