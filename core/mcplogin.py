"""MCP login without an interactive session (`hub mcp login`): `claude mcp login` needs a terminal (07.10: "stdin isn't a
terminal") - we run it in a pseudo-terminal. It opens the login in the owner's system browser itself and catches the OAuth
answer on localhost (the agents' browser is not that one: 07.10 "it opens in the wrong place"). We wait until the login finishes."""
import contextlib
import json
import os
import re
import time

# the OAuth login link: the path differs between servers (/authorize, /oauth2/auth - 23.10), the sign is an authorization
# request in the parameters: both response_type and redirect_uri (Claude Code always puts them); a lone client_id also
# occurs in an application page's address - a waiting process would take that
URL = re.compile(rb"https://[^\s\x07\x1b\]?]+\?(?=[^\s\x07\x1b\]]*\bresponse_type=)(?=[^\s\x07\x1b\]]*\bredirect_uri=)"
                 rb"[^\s\x07\x1b\]]+"
                 rb"|https://[^\s\x07\x1b\]]+/authorize\?[^\s\x07\x1b\]]+")
LOCK_WAIT = 10    # seconds the mark waits for the Claude Code settings lock
LOCK_STALE = 10   # an older lock is left by a crashed writer (Claude Code itself counts it so, proper-lockfile)


def lock_path(config):
    """The lock for editing Claude Code settings - the folder <config>.lock, the same one Claude Code itself takes
    (proper-lockfile). Without it two logins at once, or Claude Code writing in the same second, would read the old file
    and the later write would erase the earlier edit."""
    return config + ".lock"


@contextlib.contextmanager
def _locked(config):
    """Hold the settings lock (lock_path); a leftover one (older than LOCK_STALE) - take over. Not taken within LOCK_WAIT - OSError."""
    lock, end = lock_path(config), time.time() + LOCK_WAIT
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            try:
                seen = os.stat(lock)
                if time.time() - seen.st_mtime > LOCK_STALE and _take_over(lock, seen):
                    continue
            except FileNotFoundError:
                continue   # just released
            except OSError:
                pass   # a file or a non-empty folder sits where the lock is - not ours, wait for the deadline as if busy
            if time.time() >= end:
                from core import i18n
                raise OSError(i18n.t("cli.mcp.lock_busy", config=config)) from None
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def _take_over(lock, seen):
    """Remove the abandoned lock `seen`, and only it: two waiters see it at once, the first removes it and makes its
    own, and the second would remove that fresh one (both inside, one edit lost). Waiters of the hub take a leftover
    over one at a time, under a file lock of their own (released by the OS if the process dies; without the hub's
    settings folder there is none — then as before), and remove it only if it is still the very folder they saw.
    -> True: removed."""
    import platforms
    from core import catalog
    with contextlib.ExitStack() as one_at_a_time:
        try:
            os.makedirs(catalog.config_dir(), exist_ok=True)
            one_at_a_time.enter_context(platforms.current.exclusive(
                os.path.join(catalog.config_dir(), "claude-settings.lock")))
        except OSError:
            pass   # no settings folder of the hub to lock in (read-only): the look alone
        now = os.stat(lock)
        if (now.st_ino, now.st_mtime_ns) != (seen.st_ino, seen.st_mtime_ns):
            return False   # someone else's by now: wait for it
        os.rmdir(lock)
        return True


def approve(cwd, server, config=None):
    """The session folder is trusted by Claude Code (as after "Trust this folder" in interactive claude), and the login
    server from its .mcp.json is approved (enabledMcpjsonServers): without that it is "awaiting approval" and cannot be
    logged into (07.10; 17.10, claude 2.1.302 - a trust mark alone is not enough). Only the login server is approved, and
    only if it is in the folder's .mcp.json (a typo or a plugin's server do not stay approved for the future), and not
    rejected by the owner (disabledMcpjsonServers). Claude Code keeps both marks under the root of the folder's git repo
    (_git_root: for a worktree - the main repo), so they hold for the whole repository, by server name, as with an approval
    given in interactive claude. The hub raises sessions in this folder with its .mcp.json anyway. ->
    what is set now: {"trust", "server"} or a part (the caller will tell about it when the login starts; if it did not
    start - withdraw). Claude Code itself moves the approval into the folder's .claude/settings.local.json, as after an
    approval in interactive mode."""
    listed = server in _of(_read(os.path.join(cwd, ".mcp.json")).get("mcpServers"), dict)
    return _edit(cwd, server if listed and server not in rejected(cwd) else None, config, set())


def rejected(cwd):
    """The servers the owner switched off for the folder: `disabledMcpjsonServers` in the .claude/settings.local.json and
    settings.json of the folder and of the root of its repo (a rejection in interactive claude is written there). A
    value that is not a list of names counts for nothing."""
    return {s for d in {cwd, _git_root(cwd)} for n in ("settings.local.json", "settings.json")
            for s in _of(_read(os.path.join(d, ".claude", n)).get("disabledMcpjsonServers"), list)
            if isinstance(s, str)}


def project_servers(cwd):
    """The servers of the folder's .mcp.json that a session in it gets: {name: config} without the ones the owner
    switched off (launch_flags passes the file through catalog.project_mcp_file the same way)."""
    listed = _of(_read(os.path.join(cwd, ".mcp.json")).get("mcpServers"), dict)
    off = rejected(cwd)
    return {k: v for k, v in listed.items() if k not in off and isinstance(v, dict)}


def _git_root(cwd):
    """The folder Claude Code keys its project settings by (2.1.309, checked live): the root of the git repo that holds
    the folder - for a worktree the MAIN repo, for a subfolder the top of the repo; not a repo - the folder itself.
    Read from the files git keeps (no git process): `.git` is a folder, or a file `gitdir: <repo>/.git/worktrees/<name>`
    whose `commondir` leads back to `<repo>/.git`."""
    folder = os.path.realpath(cwd)
    here = folder
    while True:
        dot = os.path.join(here, ".git")
        if os.path.isdir(dot):
            return here
        if os.path.isfile(dot):
            try:
                with open(dot, encoding="utf-8") as f:
                    line = f.readline().strip()
                gitdir = os.path.join(here, line[len("gitdir:"):].strip()) if line.startswith("gitdir:") else None
                with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as f:
                    common = os.path.realpath(os.path.join(gitdir, f.read().strip()))
            except (OSError, TypeError):
                return here   # a submodule or an odd layout: the folder that holds the `.git` file
            return os.path.dirname(common) if os.path.basename(common) == ".git" else here
        parent = os.path.dirname(here)
        if parent == here or not parent:
            return folder
        here = parent


def _of(value, kind):
    return value if isinstance(value, kind) else kind()


def _read(path):
    """A JSON object from a file, or {} (no file, not JSON, not an object)."""
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def withdraw(cwd, marks, server, config=None):
    """Remove what approve() set for this login (marks is its result), if the login did not start."""
    if marks:
        _edit(cwd, server, config, set(marks))


def approved(marks, server):
    """What to tell the human about what approve() set."""
    from core import i18n
    if "trust" not in marks:
        return i18n.t("cli.mcp.approved_server", server=server)
    return i18n.t("cli.mcp.trusted_and_server", server=server) if "server" in marks else i18n.t("cli.mcp.trusted")


def _edit(cwd, server, config, undo):
    path = config or os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~"), ".claude.json")
    path = os.path.realpath(path)   # settings that are a link (a dotfiles folder) stay a link: its target is replaced
    if not os.path.isfile(path):
        return set()
    try:
        with _locked(path):   # the key is as in Claude Code: "/" on Windows too
            return _mark(path, _git_root(cwd).replace(os.sep, "/"), server, undo)
    except OSError:
        return set()


def _mark(path, folder, server, undo):
    """Under the lock: read, set the marks (undo empty) or remove those listed in undo, write a copy next to the file,
    keep the permissions, replace atomically. -> what was changed."""
    import tempfile
    import platforms
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError:
        return set()
    projects = data.setdefault("projects", {}) if isinstance(data, dict) else None
    proj = projects.setdefault(folder, {}) if isinstance(projects, dict) else None
    if not isinstance(proj, dict):
        return set()
    servers = proj.get("enabledMcpjsonServers", [])
    done = set()
    if not undo:
        if not proj.get("hasTrustDialogAccepted"):
            proj["hasTrustDialogAccepted"] = True
            done.add("trust")
        if server and isinstance(servers, list) and server not in servers and \
                server not in _of(proj.get("disabledMcpjsonServers"), list):
            proj["enabledMcpjsonServers"] = servers + [server]
            done.add("server")
    else:
        if "trust" in undo and proj.get("hasTrustDialogAccepted"):
            proj.pop("hasTrustDialogAccepted")
            done.add("trust")
        if "server" in undo and isinstance(servers, list) and server in servers:
            servers.remove(server)
            if not servers:
                proj.pop("enabledMcpjsonServers")
            done.add("server")
        if not proj:
            projects.pop(folder)
    if not done:
        return done
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, os.stat(path).st_mode)
        platforms.current.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return done


def login(server, cwd, timeout=300, argv=None, say=lambda s: print(s, flush=True), run_dir=None):
    """(exit code, the output tail without colors). argv - for tests (by default claude mcp login <server>). run_dir -
    one login per server: if a login to it is already running, we wait for its result and return that one, we do not start
    a second (16.10: the hub and a session logged in 4 s apart, the second client registration overwrote the first's
    client_id - "Client ID ... does not match")."""
    if run_dir is None:
        return _login(server, cwd, timeout, argv, say)
    import platforms
    os.makedirs(run_dir, exist_ok=True)
    base = os.path.join(run_dir, _safe(server))
    asked = time.time()
    with open(base + ".lock", "a+") as f:
        try:
            platforms.current.try_lock(f)
        except OSError:
            from core import i18n
            say(i18n.t("cli.mcp.login_running", server=server))
            with platforms.current.exclusive(base + ".lock"):
                try:
                    with open(base + ".result", encoding="utf-8") as r:
                        got = json.load(r)
                    if got.get("at", 0) >= asked:   # the result of the login that was running, not of an earlier one
                        return got["code"], got["out"]
                except (OSError, ValueError, KeyError):
                    pass
            return 1, i18n.t("cli.mcp.ended_no_result")
        code, out = _login(server, cwd, timeout, argv, say)
        _write(base + ".result", json.dumps({"code": code, "out": out, "at": time.time()}, ensure_ascii=False))
        return code, out


def _login(server, cwd, timeout, argv, say):
    import platforms
    marks = set() if argv else approve(cwd, server)
    try:
        pid, fd = platforms.current.spawn_tty(argv or ["claude", "mcp", "login", server], cwd)
    except OSError as e:   # the terminal or claude did not start: the login did not begin - the folder stays as it was
        withdraw(cwd, marks, server)
        return 1, str(e)
    out, opened, told, end, code = b"", False, False, time.time() + timeout, None
    try:
        while time.time() < end:
            out += platforms.current.tty_read(fd, 0.5)
            if marks and out and not told:   # claude started talking - the login began
                told = True
                say(approved(marks, server))
            m = URL.search(out)
            if m and not opened:
                opened = True
                from core import i18n
                say(i18n.t("cli.mcp.opened_browser", url=m.group(0).decode()))
            code = platforms.current.tty_poll(pid)
            if code is not None:
                break
        else:
            platforms.current.tty_stop(pid)
            code = 124
    finally:
        platforms.current.tty_close(fd)
    if not told:   # claude never started (not in PATH: the terminal forks first - that is the exit code)
        withdraw(cwd, marks, server)
    return code, _plain(out)[-600:]


def _plain(out):
    """Terminal output without colors and service sequences (OSC: links, the window title - sent by ConPTY on Windows)."""
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", out.decode("utf-8", "replace"))


def has_browser(env=None):
    """Whether the hub has a browser in which the human will see the login (platforms: macOS and Windows - yes, Linux - with
    a display). Without one (a server, 11.10) `claude mcp login` opened the login where nobody sees it - the login goes by a link."""
    import platforms
    return platforms.current.has_browser(os.environ if env is None else env)


def _safe(name):
    return re.sub(r"[^\w.-]", "_", name)


def login_dir(state_dir, session, server):
    """An MCP from the hub catalog (or from a plugin) is given to the session by the core with its own --mcp-config
    (sessions/<name>/mcp.json), and `claude mcp login` in the session folder does not see it ("No MCP server named
    <name>", 11.10). The login is done from a folder of its own with the same server description (Claude Code stores the
    login by name and description: the session will pick it up), and the server is approved in it. The server is not in
    the session's mcp.json (it comes from the repo's .mcp.json) - None: login from the session folder, as before."""
    try:
        with open(os.path.join(state_dir, "sessions", session, "mcp.json"), encoding="utf-8") as f:
            srv = (json.load(f).get("mcpServers") or {}).get(server)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(srv, dict):
        return None
    d = os.path.join(state_dir, "mcp-login", "folders", _safe(server))
    os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
    from core.catalog import write_private   # here: as a script (__main__) the path to the hub code is set below
    write_private(os.path.join(d, ".mcp.json"), {"mcpServers": {server: srv}})   # the same env as in the session's mcp.json
    with open(os.path.join(d, ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
        json.dump({"enableAllProjectMcpServers": True}, f)
    return d


def work_dir(state_dir, server):
    """Where link logins of one server wait: each has its own folder (pid, url, redirect, result), `current` names the
    last one."""
    return os.path.join(state_dir, "mcp-login", "wait", _safe(server))


# --- a sign-in from the page (core/mcpwatch): what must not reach the journal lives in files for the owner only --------
# links/<attempt> — the sign-in link (its state parameter lets whoever holds it finish that sign-in); inbox/<attempt> —
# the address from the browser bar after the sign-in (it carries a one-time code): the core takes it and deletes the file.
# Files older than FILE_STALE are removed on the next write (an attempt lives ~5 minutes).
FILE_STALE = 900
ATTEMPT = re.compile(r"l[0-9]{1,20}")   # the attempt id: "l" and the seq of the request (core/mcpwatch)
RETURN = re.compile(r"https?://\S{1,4000}")   # the address from the browser bar after a sign-in (the page, `hub mcp login --redirect`)


def _box(state_dir, kind, attempt):
    if not isinstance(attempt, str) or not ATTEMPT.fullmatch(attempt):
        raise ValueError("attempt")
    return os.path.join(state_dir, "mcp-login", kind, attempt)


def _put(state_dir, kind, attempt, url):
    from core.catalog import write_private   # here: as a script (__main__) the path to the hub code is set below
    import platforms
    path = _box(state_dir, kind, attempt)
    folder = os.path.dirname(path)
    platforms.current.makedirs_private(folder)
    now = time.time()
    for n in os.listdir(folder):
        try:
            if now - os.stat(os.path.join(folder, n)).st_mtime > FILE_STALE:
                os.unlink(os.path.join(folder, n))
        except OSError:
            pass
    write_private(path, {"url": url})


def _get(state_dir, kind, attempt, take=False):
    try:
        path = _box(state_dir, kind, attempt)
        with open(path, encoding="utf-8") as f:
            url = json.load(f).get("url")
        if take:
            os.unlink(path)
    except (OSError, ValueError, AttributeError):
        return None
    return url if isinstance(url, str) and url else None


def scrub(text):
    """A text that may repeat the address after a sign-in (Claude Code's error, the terminal's output) — without its
    one-time values (percent-encoded too: decoded first)."""
    from urllib.parse import unquote
    return re.sub(r"(?<![A-Za-z_])(code|state|code_verifier|access_token|refresh_token)(=|[\"']?\s*:\s*[\"']?)[^&\s\"',}]+",
                  r"\1\2…", unquote(str(text or "")))


def outcome(code, out):
    """A finished login the old way as a step fact: ok, or why not (the end of the terminal's output, scrubbed); the fact
    goes to the journal — in the owner's language."""
    from core import i18n
    return {"ok": code == 0, **({} if code == 0 else {"error": scrub(str(out or "").strip())[-300:]
                                                     or i18n.tr(i18n.owner(), "cli.mcp.exit_code", code=code)})}


def ended(work):
    """(code, output) of the last link login of the server once its waiting process has finished — the human signed in
    and the browser came back to Claude Code's own listener, or it ended otherwise; None — it still waits (or there is none)."""
    run = _run(work)
    try:
        with open(os.path.join(run, "result"), encoding="utf-8") as f:
            r = json.load(f)
        return r.get("code", 1), r.get("out") or ""
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def put_link(state_dir, attempt, url):
    _put(state_dir, "links", attempt, url)


def link_of(state_dir, attempt):
    """The sign-in link of the attempt, or None (none yet, or it went stale)."""
    return _get(state_dir, "links", attempt)


def put_callback(state_dir, attempt, url):
    _put(state_dir, "inbox", attempt, url)


def take_callback(state_dir, attempt):
    """The address after the sign-in for the attempt — once: the file is deleted. None — there is none."""
    return _get(state_dir, "inbox", attempt, take=True)


def drop_callback(state_dir, attempt):
    """The core refused the address (core.rejected): it does not wait in the folder."""
    try:
        os.unlink(_box(state_dir, "inbox", attempt))
    except (OSError, ValueError):
        pass


_RUN = re.compile(r"[0-9a-f]{16,32}")


def _run(work):
    """The folder of the last link login, or None."""
    try:
        with open(os.path.join(work, "current"), encoding="utf-8") as f:
            name = f.read().strip()
    except OSError:
        return None
    return os.path.join(work, name) if _RUN.fullmatch(name) else None


def _pid(run):
    """The pid of a login that is still waiting in its folder, or None (finished, or there was none)."""
    import platforms
    try:
        with open(os.path.join(run, "pid")) as f:
            pid = int(f.read())
    except (OSError, ValueError, TypeError):
        return None
    if os.path.exists(os.path.join(run, "result")) or not platforms.current.alive(pid):
        return None
    return pid


def _waiting(work):
    """The pid of the last login if it is waiting, or None."""
    run = _run(work)
    return _pid(run) if run else None


def _replaced(run):
    """A newer login of the same server began - this one is no longer needed."""
    current = _run(os.path.dirname(run))
    return current is not None and os.path.basename(current) != os.path.basename(run)


def _write(path, text):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    import platforms   # here, not on top: the waiting process starts as a script (see __main__), the path is set only there
    platforms.current.replace(path + ".tmp", path)


def start(server, cwd, work, wait=60, say=lambda s: print(s, flush=True)):
    """A link login (a hub without a browser): `claude mcp login --no-browser` in the background - a waiting process holds
    it (Claude Code waits for an answer ~5 minutes), the link is returned at once. -> (link, None) or (None, why not). The
    earlier waiting login of the same server leaves by itself on seeing the new one (_replaced): we kill nothing by pid -
    there may already be another user process under it; and each login writes only to its own folder - the old one will
    not hand out its link and will not take the new one's address."""
    import shutil
    import uuid
    os.makedirs(work, exist_ok=True)
    name = uuid.uuid4().hex
    os.makedirs(os.path.join(work, name))
    _write(os.path.join(work, "current"), name)
    for n in os.listdir(work):   # folders of finished logins; files of the earlier layout (one folder per server)
        path = os.path.join(work, n)
        if n in ("url", "redirect", "result", "pid"):
            try:
                os.unlink(path)
            except OSError:
                pass
        elif n != name and _RUN.fullmatch(n) and not _pid(path):
            shutil.rmtree(path, ignore_errors=True)
    marks = approve(cwd, server)
    url, why = _link(server, cwd, os.path.join(work, name), wait)
    if url:
        if marks:
            say(approved(marks, server))
    else:   # no link - the login did not begin: the waiting process leaves by itself (a cancel mark in its folder), the folder stays as it was
        _write(os.path.join(work, name, "cancel"), "")
        withdraw(cwd, marks, server)
    return url, why


def _link(server, cwd, run, wait):
    """Start the waiting process in its folder and wait for the link -> (link, None) or (None, why not)."""
    import subprocess
    import sys
    import platforms
    from core import i18n
    p = platforms.current.popen_detached([sys.executable, os.path.abspath(__file__), "wait", server, cwd, run],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _write(os.path.join(run, "pid"), str(p.pid))
    end = time.time() + wait
    while time.time() < end:
        if os.path.exists(os.path.join(run, "url")):
            with open(os.path.join(run, "url"), encoding="utf-8") as f:
                return f.read().strip(), None
        if os.path.exists(os.path.join(run, "result")):
            with open(os.path.join(run, "result"), encoding="utf-8") as f:
                return None, (json.load(f).get("out") or "").strip()[-400:] or i18n.t("cli.mcp.ended_no_link")
        p.poll()
        time.sleep(0.2)
    return None, i18n.t("cli.mcp.no_link_in", wait=wait)


def finish(work, redirect, wait=60):
    """The address from the browser bar after the login (http://localhost:.../callback?code=...) - to the last waiting login;
    -> (code, output)."""
    from core import i18n
    run = _run(work)
    if not run or not _pid(run):
        return 1, i18n.t("cli.mcp.not_waiting_address")
    _write(os.path.join(run, "redirect"), redirect.strip())
    end = time.time() + wait
    while time.time() < end:
        try:
            with open(os.path.join(run, "result"), encoding="utf-8") as f:
                r = json.load(f)
            return r.get("code", 1), r.get("out") or ""
        except (OSError, ValueError):
            time.sleep(0.2)
    return 124, i18n.t("cli.mcp.no_answer_in", wait=wait)


def _wait(server, cwd, run, timeout=900):
    """The waiting link-login process, in its own folder: claude mcp login --no-browser in a pseudo-terminal; the link goes
    to url, the return address from redirect goes to its input, the result goes to result. A newer login began (_replaced)
    or this one was cancelled (cancel) - claude is stopped, the folder is removed."""
    import shutil
    import platforms
    try:
        pid, fd = platforms.current.spawn_tty(["claude", "mcp", "login", "--no-browser", server], cwd)
    except OSError as e:   # the terminal or claude did not start: say so at once, not wait for start() to time out
        _write(os.path.join(run, "result"), json.dumps({"code": 1, "out": str(e)}, ensure_ascii=False))
        return
    out, sent, end, code = b"", False, time.time() + timeout, None
    try:
        while time.time() < end:
            out += platforms.current.tty_read(fd, 0.5)
            if _replaced(run) or os.path.exists(os.path.join(run, "cancel")):
                platforms.current.tty_stop(pid)
                shutil.rmtree(run, ignore_errors=True)
                return
            m = URL.search(out)
            if m and not os.path.exists(os.path.join(run, "url")):
                _write(os.path.join(run, "url"), m.group(0).decode())
            if not sent and os.path.exists(os.path.join(run, "redirect")):
                with open(os.path.join(run, "redirect"), encoding="utf-8") as f:
                    platforms.current.tty_write(fd, f.read().strip().encode() + b"\r")
                sent = True
            code = platforms.current.tty_poll(pid)
            if code is not None:
                break
        else:
            platforms.current.tty_stop(pid)
            code = 124
    finally:
        platforms.current.tty_close(fd)
    text = _plain(out)
    _write(os.path.join(run, "result"), json.dumps({"code": code, "out": text[-600:]}, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if sys.argv[1:2] == ["wait"]:
        _wait(*sys.argv[2:5])
