#!/usr/bin/env python3
"""Compatibility of the core with Claude Code versions — a check by the publisher before a release (not needed by every hub).

Each version is started with the same command line the core builds (hosts/streamjson.StreamHost.command and
core.catalog.launch_flags: stream-json, --settings with hooks, the agent's --append-system-prompt-file with the core's
addition, --system-prompt-snapshot if the version knows it, --effort, --model), in an empty temporary folder, without
MCP; one turn on a cheap model. The result per version: ok / refused (what claude said) / no answer in N s / core hooks
did not fire.

    tools/compat.py [--versions 2.1.218,…] [--timeout 120] [--ssh user@host] [--json]
    tools/compat.py --pin <tree>

The version by default is one: the one Anthropic's stable channel names now (downloads.claude.ai
claude-code-releases/stable — what `hub setup claude` installs and the hubs' claude updates itself to; owner decision
2026-11-11: test only on the latest Claude Code at the time of the release, and a hub that needs a newer one installs
it itself when it updates). --pin <tree> writes that version into <tree>/core/version.py CLAUDE_TESTED: publish.sh does it
in every snapshot, the release tag tests the snapshot's CLAUDE_TESTED, and a hub whose claude is older installs it while
it updates (modules/update).

The same from the environment (publish.sh sets them this way): HUB_COMPAT_VERSIONS, HUB_COMPAT_SSH, HUB_COMPAT_SSH_DIR.
The second machine sleeps until needed (a Windows virtual machine eats a lot of memory): HUB_COMPAT_SSH_WAKE is the
command that wakes it (the check then waits for ssh for up to 10 min), HUB_COMPAT_SSH_SLEEP puts it back to sleep after
the check.

Versions — the stable one or --versions; a binary from ~/.local/share/claude/versions, a missing one is installed with
`npm i` into a temporary folder — the owner's active claude is not touched. --ssh (or HUB_COMPAT_SSH) — the same run on another
machine (Windows): a snapshot of HEAD is put into HUB_COMPAT_SSH_DIR (default C:\\hubrev\\compat), its versions are
checked. The machine is unreachable — "not checked", not a failure. Exit code: 0 — all ok, 3 — incompatible versions
found, 2 — the check itself failed.
"""
import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PROMPT = "answer in one word: ok"
MODEL = "haiku"
VERSIONS_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "claude", "versions")
HOOKS = ("session.started", "turn.started", "turn.stopped")   # without them the core does not see session turns
OK, REFUSED, TIMEOUT, HOOKLESS, ERROR, SKIPPED = "ok", "refused", "timeout", "hooks", "error", "skipped"
BAD = (REFUSED, TIMEOUT, HOOKLESS)
CHANNEL = "stable"   # the channel hubs install (core/claudeinstall) and their claude follows
PIN = re.compile(r'^(CLAUDE_TESTED\s*=\s*)"[^"\n]*"', re.M)   # not MIN_CLAUDE = "…": core/version.py says why


def vkey(version):
    return tuple(int(x) for x in re.findall(r"\d+", version))


def is_version(name):
    return bool(re.fullmatch(r"\d+(\.\d+)+", name))


def local_versions(directory):
    """{version: binary} from the Claude Code versions folder (one file per version)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return {}
    return {n: os.path.join(directory, n) for n in names if is_version(n) and os.path.isfile(os.path.join(directory, n))}


def install(version, prefix, npm="npm"):
    """A version from npm into a temp folder → (binary, None) or (None, error). `claude install` will not do: it
    switches the owner's active claude."""
    try:
        r = subprocess.run([npm, "i", "--prefix", prefix, "--no-save", "--no-audit", "--no-fund",
                            f"@anthropic-ai/claude-code@{version}"], capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"npm: {e}"
    for name in ("claude.cmd", "claude") if os.name == "nt" else ("claude",):
        path = os.path.join(prefix, "node_modules", ".bin", name)
        if r.returncode == 0 and os.path.exists(path):
            return path, None
    return None, "npm: " + tail(r.stderr or r.stdout or f"code {r.returncode}")


def reported(binary):
    """The version the binary itself reports (`2.1.291 (Claude Code)` → 2.1.291)."""
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    found = re.match(r"\s*(\d+(?:\.\d+)+)", out)
    return found.group(1) if found else ""


def stable():
    """The version Anthropic's stable channel names now ("2.1.330"), read as the hubs' installer reads it
    (core/claudeinstall); OSError — no answer, a refusal, or not a plain version."""
    from core import claudeinstall
    with tempfile.TemporaryDirectory(prefix="hub-compat-channel-") as tmp:   # its step log, not the hub's
        try:
            got = claudeinstall.Installer(tmp).resolve(CHANNEL)
        except claudeinstall.Failed as e:
            raise OSError(f"Claude Code {CHANNEL}: {e}") from None
    if not re.fullmatch(r"\d+\.\d+\.\d+", got):   # CLAUDE_TESTED is read as digits and dots only
        raise OSError(f"Claude Code {CHANNEL}: not a version: {got[:80]!r}")
    return got


def pin(tree, version):
    """CLAUDE_TESTED = version in <tree>/core/version.py (the line's value only); OSError — no such line."""
    path = os.path.join(tree, "core", "version.py")
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    new, count = PIN.subn(lambda m: f'{m.group(1)}"{version}"', text)
    if count != 1:
        raise OSError(f"{path}: {count} CLAUDE_TESTED lines, not one")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new)


def tail(text, lines=3, limit=300):
    out = " · ".join(x.strip() for x in str(text).strip().splitlines()[-lines:] if x.strip())
    return out if len(out) <= limit else "…" + out[-limit:]


def command(binary, work):
    """The core command line for one turn: an agent (its own file + the core addendum), no tools, haiku."""
    from core import catalog
    from hosts.streamjson import StreamHost
    state = os.path.join(work, "state")
    agent = os.path.join(work, "agent.md")
    os.makedirs(state, exist_ok=True)
    with open(agent, "w", encoding="utf-8") as f:
        f.write("# Agent maps\n\nCompatibility check: answer briefly.\n")
    host = StreamHost(state, claude=(binary,))
    cmd = host.command("compat", str(uuid.uuid4()), agent_file=agent, agents=[("maps", "maps")], executor_of="maps")
    cmd += catalog.launch_flags([], {}, {}, "work", os.path.join(work, "cwd"), "all", state, "compat",
                                effort=catalog.DEFAULTS.get("effort") or "medium", model=MODEL)
    return cmd, state


def _kill(popen):
    """claude with what it started: its group (POSIX; once claude is collected, only while no process holds its
    number), its tree by start times (Windows)."""
    import platforms
    try:
        platforms.current.kill_detached(popen, hard=True)
    except OSError:
        pass


def hook_kinds(state):
    from core import spool
    return {item.get("type") for _, item in spool.take(os.path.join(state, "spool"))}


def pool_account(env):
    """The claude login — as for sessions: the pool account they are on now (the computer has no login of its own, all versions
    refused with "Not logged in"). No pool or it is paused — this computer's login (changes env). The login goes through the
    core/profiles.py facade (a pool in the core tree or a login module)."""
    from core import profiles
    state, active = active_account()
    return profiles.model_env(env, active, state)


def active_account():
    """(the state folder, the pool account the sessions are on now or None)."""
    from core import daemon
    state = daemon.default_state_dir()
    try:
        with open(os.path.join(state, "state.json"), encoding="utf-8") as f:
            active = ((json.load(f).get("state") or {}).get("account") or {}).get("active")
    except (OSError, ValueError, AttributeError):
        active = None
    return state, active


def ssh_token():
    """The claude login for the second machine: the real token of the active pool account (None — no token of its own, the
    machine's login). pool_account is not for this: with the accounts gateway the core's environment holds the gateway's
    stub, and the second machine has no gateway (2026-10-30, 401 on every version). The value is never printed."""
    from core import profiles
    state, active = active_account()
    got = profiles.Profiles(state).token(active, None, "model") if active else None
    token = ((got or {}).get("env") or {}).get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        token = pool_account({}).get("CLAUDE_CODE_OAUTH_TOKEN")   # as before (it also raises NoProfile)
    if token == profiles.STUB:
        raise OSError("the active account has no real token — only the gateway stub, and the second machine has no gateway")
    return token


def use_token(env, token):
    """Log claude in with a token that came from outside (--token-stdin: an account of the pool of the machine that checks over ssh) —
    instead of this machine's login (changes env)."""
    if token:
        from core.modlaunch import AUTH_ENV
        for k in AUTH_ENV:
            env.pop(k, None)
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


TOKEN = None   # --token-stdin


def check(binary, timeout=120, prompt=PROMPT):
    """One turn of a version → {"status", "detail", "seconds"}."""
    from core import catalog
    import platforms
    work = tempfile.mkdtemp(prefix="hub-compat-")
    try:
        os.makedirs(os.path.join(work, "cwd"))
        cfg = os.path.join(work, "hub.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("{}")
        old = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = cfg   # settings of the live hub (critic etc.) do not change the check
        try:
            cmd, state = command(binary, work)
        finally:
            os.environ.pop("HUB_CONFIG") if old is None else os.environ.__setitem__("HUB_CONFIG", old)
        env = {"MCP_TIMEOUT": catalog.MCP_TIMEOUT, **platforms.current.owner_env(os.environ), **catalog.user_env(),
               "HUB_SESSION": "compat", "HUB_STATE_DIR": state}
        env.pop("CLAUDECODE", None)   # not a "nested" claude if the check is started from a session
        pool_account(env)
        use_token(env, TOKEN)
        started = time.time()
        err_path = os.path.join(work, "stderr.log")
        with open(err_path, "w", encoding="utf-8") as err:
            popen = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1, cwd=os.path.join(work, "cwd"),
                                     env=env, start_new_session=os.name == "posix")
        lines = queue.Queue()

        def read():
            for line in popen.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        result, said = None, []
        try:
            popen.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}},
                                         ensure_ascii=False) + "\n")
            popen.stdin.flush()
        except OSError:
            pass
        deadline = started + timeout
        while result is None:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                said.append(line.strip())
                continue
            if msg.get("type") == "result":
                result = msg
        seconds = round(time.time() - started, 1)
        if result is not None:
            try:
                popen.stdin.close()
            except OSError:
                pass
            try:
                popen.wait(timeout=20)   # Stop/SessionEnd — hooks after the reply
            except subprocess.TimeoutExpired:
                pass
        _kill(popen)
        popen.wait()
        reader.join(5)
        for pipe in (popen.stdin, popen.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        with open(err_path, encoding="utf-8", errors="replace") as f:
            stderr = f.read()
        if result is None:
            if time.time() >= deadline:
                return {"status": TIMEOUT, "detail": f"no answer within {timeout} s", "seconds": seconds}
            return {"status": REFUSED, "detail": tail(stderr or "\n".join(said) or f"exited with code {popen.returncode}"),
                    "seconds": seconds}
        if result.get("is_error") or result.get("subtype") != "success":
            return {"status": REFUSED, "detail": tail(result.get("result") or result.get("subtype") or stderr),
                    "seconds": seconds}
        missing = [k for k in HOOKS if k not in hook_kinds(state)]
        if missing:
            return {"status": HOOKLESS, "detail": "the core's hooks did not fire: " + ", ".join(missing), "seconds": seconds}
        return {"status": OK, "detail": tail(result.get("result") or "", 1, 60), "seconds": seconds}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_local(versions, directory=VERSIONS_DIR, timeout=120, npm="npm", log=None):
    """{version: result} for the versions: the binary from the folder, a missing one from npm."""
    found = local_versions(directory)
    out = {}
    with tempfile.TemporaryDirectory(prefix="hub-compat-npm-") as tmp:
        for version in sorted(set(versions), key=vkey):
            binary = found.get(version)
            if not binary:
                binary, problem = install(version, os.path.join(tmp, version), npm)
                if problem:
                    out[version] = {"status": ERROR, "detail": problem}
                    continue
            if log:
                log(f"{version}…")
            said = reported(binary)
            if said != version:
                out[version] = {"status": ERROR, "detail": f"this is not {version}: --version → {said or 'nothing'}"}
                continue
            try:
                out[version] = check(binary, timeout)
            except Exception as e:   # a failure of the check itself is not a verdict on the version
                out[version] = {"status": ERROR, "detail": f"{type(e).__name__}: {e}"}
            gate = gateway(binary)
            if gate is not None:   # a tree without the gateway: no line about it
                out[version]["gateway"] = gate
    return out


def gateway(binary):
    """The accounts gateway (modules/gateway) on this version: a self-check, as the gateway does at startup (tool search in init
    and in a request through the gateway, the "first-party" flag — only if it takes nothing away). No network and no login: the API is fake.
    Not a verdict on the version — the gateway is off by default and on such a version simply will not turn on."""
    from core import cutout
    if not cutout.present("gateway"):   # the open flavor is cut without it (core/cutout.py)
        return None   # nothing to say about a gateway the tree does not carry (the report leaves it out)
    from modules.gateway import gateway as gw
    import platforms
    try:
        got = gw.probe([binary], platforms.current.owner_env(os.environ))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    return {k: got[k] for k in ("ok", "first_party", "lost", "why") if k in got}


def run_ssh(target, folder, timeout=120, ssh="ssh", scp="scp", token=None, wake=None, sleep=None, wake_wait=600,
            poll=10, versions=()):
    """run_once, but a sleeping machine is woken with the wake command, we wait for ssh and put it back to sleep with the
    sleep command after the check (the Windows machine is powered on only for tests, it eats a lot of memory)."""
    if not wake:
        try:
            return run_once(target, folder, timeout, ssh, scp, token, versions)
        finally:
            if sleep:
                subprocess.run(sleep, shell=True, capture_output=True, timeout=600)
    try:
        r = subprocess.run(wake, shell=True, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return {"": {"status": ERROR, "detail": "could not wake it: " + tail(r.stderr or r.stdout)}}
        end = time.time() + wake_wait
        while True:
            probe = subprocess.run([ssh, "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", target, "echo ok"],
                                   capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            if probe.returncode == 0:
                break
            if time.time() > end:
                return {"": {"status": ERROR, "detail": "woke it, but ssh did not answer: " + tail(probe.stderr)}}
            time.sleep(poll)
        return run_once(target, folder, timeout, ssh, scp, token, versions)
    finally:
        if sleep:
            subprocess.run(sleep, shell=True, capture_output=True, timeout=600)


def run_once(target, folder, timeout=120, ssh="ssh", scp="scp", token=None, versions=()):
    """The same run on another machine: the HEAD snapshot goes there, `tools/compat.py --json` runs there → {version: result} or
    {"": skipped} — the machine is unavailable. token — the claude login there (an account of this machine's pool): into ssh stdin, not
    onto the command line and not onto that machine's disk."""
    def skipped(why):
        return {"": {"status": SKIPPED, "detail": why}}
    opts = ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    try:
        probe = subprocess.run([ssh, *opts, target, "echo ok"], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return skipped(f"ssh: {e}")
    if probe.returncode != 0:
        return skipped(tail(probe.stderr) or f"ssh: code {probe.returncode}")
    with tempfile.TemporaryDirectory(prefix="hub-compat-ssh-") as tmp:
        archive = os.path.join(tmp, "compat.zip")
        subprocess.run(["git", "-C", REPO, "archive", "--format=zip", "-o", archive, "HEAD"], check=True)
        win = "\\" in folder or ":" in folder
        zip_remote = folder.rstrip("\\/") + ".zip"
        r = subprocess.run([scp, "-q", *opts, archive, f"{target}:{zip_remote.replace(chr(92), '/')}"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return {"": {"status": ERROR, "detail": "scp: " + tail(r.stderr)}}
    flag = (" --token-stdin" if token else "") + (f" --versions {','.join(versions)}" if versions else "")
    if win:
        remote = (f'(if exist "{folder}" rmdir /s /q "{folder}") & mkdir "{folder}" && tar -xf "{zip_remote}" -C '
                  f'"{folder}" && cd /d "{folder}" && set PYTHONUTF8=1&& python tools\\compat.py --json --timeout {timeout}{flag}')
    else:
        remote = (f'rm -rf "{folder}" && mkdir -p "{folder}" && cd "{folder}" && unzip -q "{zip_remote}" && '
                  f'python3 tools/compat.py --json --timeout {timeout}{flag}')
    try:
        r = subprocess.run([ssh, *opts, target, remote], capture_output=True, timeout=timeout * 10 + 120,
                           input=(token + "\n").encode() if token else b"")
    except subprocess.TimeoutExpired:
        return {"": {"status": ERROR, "detail": "the run on the machine did not finish"}}
    text = r.stdout.decode("utf-8", "replace")
    found = re.search(r"^\{.*\}\s*$", text, re.M | re.S)
    try:
        results = json.loads(found.group(0))["results"]
        # the bench had no claude — an empty result, the machine vanished from the table, and the release went on
        return results or {"": {"status": ERROR, "detail": "the machine has no claude version at all"}}
    except (AttributeError, ValueError, KeyError):
        return {"": {"status": ERROR, "detail": tail(r.stderr.decode("utf-8", "replace") or text or f"code {r.returncode}")}}


LABEL = {OK: "ok", REFUSED: "refused", TIMEOUT: "no answer", HOOKLESS: "no hooks", ERROR: "check failed",
         SKIPPED: "not checked"}


def verdict(machines):
    """{machine: {version: result}} → (exit code, incompatible [(machine, version, result)], minimum version or None).
    The minimum is the oldest version from which all checked versions passed on all machines."""
    bad = [(m, v, r) for m, res in machines.items() for v, r in res.items() if r["status"] in BAD]
    errors = [(m, v, r) for m, res in machines.items() for v, r in res.items() if r["status"] == ERROR]
    status = {}
    for res in machines.values():
        for v, r in res.items():
            if v and r["status"] != SKIPPED:
                status[v] = status.get(v, True) and r["status"] == OK
    minimum = None
    for v in sorted(status, key=vkey, reverse=True):
        if not status[v]:
            break
        minimum = v
    return (3 if bad else 2 if errors or not status else 0), bad, minimum


def table(machines):
    rows = [("machine", "version", "result", "s", "details")]
    for m, res in machines.items():
        for v in sorted(res, key=vkey):
            r = res[v]
            rows.append((m, v or "—", LABEL.get(r["status"], r["status"]), str(r.get("seconds", "")), r.get("detail", "")))
    width = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row[:4], width)) + "  " + row[4] for row in rows)


def report(machines):
    code, bad, minimum = verdict(machines)
    lines = [table(machines), ""]
    if bad:
        lines.append("incompatible: " + ", ".join(f"{v} ({m}: {LABEL[r['status']]})" for m, v, r in bad))
    if code == 2:
        lines.append("the check did not complete (see \"check failed\")")
    if minimum:
        lines.append(f"CLAUDE_TESTED can be set to {minimum}")
    gates = [(m, v, r["gateway"]) for m, res in machines.items() for v, r in res.items() if r.get("gateway")]
    if gates:
        no = [f"{v} ({m}: {g.get('why') or 'no tool search'})" for m, v, g in gates if not g.get("ok")]
        flag = sorted({v for _, v, g in gates if g.get("ok") and g.get("first_party")}, key=vkey)
        lines.append("accounts gateway: " + (f"will not turn on for {', '.join(no)}" if no else "tool search through the "
                                            "gateway is there on all") + (f"; the \"first party\" flag is taken on {', '.join(flag)}"
                                                                         if flag else "; the \"first party\" flag: nowhere"))
    return code, "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--versions", default=os.environ.get("HUB_COMPAT_VERSIONS", ""), help="the versions, comma-separated "
                    "(by default the stable channel's; the missing ones come from npm into a temp folder)")
    ap.add_argument("--versions-dir", default=VERSIONS_DIR)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--ssh", default=os.environ.get("HUB_COMPAT_SSH", ""), help="user@host of the second machine (Windows)")
    ap.add_argument("--ssh-dir", default=os.environ.get("HUB_COMPAT_SSH_DIR", "C:\\hubrev\\compat"))
    ap.add_argument("--json", action="store_true", help="the result as one line of JSON (for the run over ssh)")
    ap.add_argument("--pin", metavar="TREE", help="write the stable channel's version into TREE/core/version.py "
                    "CLAUDE_TESTED (publish.sh, into the snapshot) and exit")
    ap.add_argument("--token-stdin", action="store_true", help="claude's login: the token on the first line of stdin (how "
                    "the run over ssh passes it)")
    a = ap.parse_args(argv)
    global TOKEN
    if a.token_stdin:
        TOKEN = sys.stdin.readline().strip() or None
    if a.pin:
        try:
            got = stable()
            pin(a.pin, got)
        except OSError as e:
            print(f"--pin: {e}", file=sys.stderr)
            return 2
        print(f"CLAUDE_TESTED = {got} (Claude Code {CHANNEL})")
        return 0
    extra = [v.strip() for v in a.versions.split(",") if v.strip()]
    if any(not is_version(v) for v in extra):
        print(f"--versions: not a version: {a.versions}", file=sys.stderr)
        return 2
    off = "the model is off (HUB_NO_MODEL): not launching real claude versions"
    if os.environ.get("HUB_NO_MODEL") and (a.versions_dir == VERSIONS_DIR or a.ssh):
        print(off, file=sys.stderr)
        return 2
    if not extra:
        try:
            extra = [stable()]
        except OSError as e:
            print(f"the Claude Code {CHANNEL} version: {e}", file=sys.stderr)
            return 2
    if os.environ.get("HUB_NO_MODEL") and set(extra) - set(local_versions(a.versions_dir)):   # npm would bring a real one
        print(off, file=sys.stderr)
        return 2
    log = None if a.json else (lambda s: print(s, file=sys.stderr, flush=True))
    machines = {}
    remote = {}
    worker = None
    if a.ssh:
        try:
            token = ssh_token()
        except OSError as e:   # the login module gave no profile login (core/profiles.NoProfile)
            print(f"login for the check over ssh: {e}", file=sys.stderr)
            return 2
        wake, sleep = os.environ.get("HUB_COMPAT_SSH_WAKE"), os.environ.get("HUB_COMPAT_SSH_SLEEP")
        worker = threading.Thread(target=lambda: remote.update(run_ssh(a.ssh, a.ssh_dir, a.timeout, token=token,
                                                                       wake=wake, sleep=sleep, versions=extra)),
                                  daemon=True)
        worker.start()
    machines[sys.platform] = run_local(extra, a.versions_dir, a.timeout, log=log)
    if worker:
        worker.join()
        machines[a.ssh.split("@")[-1]] = remote
    elif not a.json:
        machines["ssh"] = {"": {"status": SKIPPED, "detail": "the second machine is not set (--ssh / HUB_COMPAT_SSH)"}}
    code, text = report(machines)
    if a.json:
        print(json.dumps({"code": code, "results": machines[sys.platform]}))
    else:
        print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
