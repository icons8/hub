#!/usr/bin/env python3
"""One step of «Setup» (installer spec §6.1, §8, §9) in its own process, so a page restart does not cut it off:

    job.py <state folder> <step>      step: claude | claude_upgrade | codex | login | chrome | github | access | doctor |
                                            voice

The step's state is `<state>/setup/<step>.json` (0600, written whole): running | done | failed | cancelled, the last
words for a person, the progress and what the step found (a link, a device code, a version). The page reads it through
/api/setup (modules/setup/setup.py) and talks back through two files: `<step>.in` (a code the person pasted) and
`<step>.cancel`. One process per step (a lock); the log is `<state>/logs/setup.log`, without secrets."""
import os
import re
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import platforms  # noqa: E402
from core import catalog, i18n, redact  # noqa: E402

STEPS = ("claude", "claude_upgrade", "codex", "login", "chrome", "github", "access", "doctor", "voice")
LOGIN_LIFE = 600     # s: a Claude sign-in waits this long for the person (the link and its local port live with it)
GITHUB_LIFE = 900    # s: GitHub's device code lives 15 minutes
INVITE_LIFE = 900    # s: after the sign-in, access is looked for this long (the invitation accepted on github.com)
INVITE_EVERY = 10    # s: between access checks (spec §9: "a check every 10 s")
TICK = 0.3
# a sign-in link Claude Code prints or hands to BROWSER: an OAuth authorize address on Anthropic's hosts only
CLAUDE_URL = re.compile(r"https://(?:[a-z0-9-]+\.)*(?:claude\.com|claude\.ai|anthropic\.com)/[^\s\"'<>]*authorize\?[^\s\"'<>]+")
DEVICE_CODE = re.compile(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")
DEVICE_URL = "https://github.com/login/device"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
ARG = re.compile(r"[A-Za-z0-9][\w.-]{0,79}")   # a step's argument: the voice model's catalog id


def folder(state):
    return os.path.join(state, "setup")


def status_path(state, step):
    return os.path.join(folder(state), f"{step}.json")


class Job:
    """The step's status file and the two files the page writes; clock — time.time (tests move it)."""

    def __init__(self, state, step, clock=time.time):
        self.state, self.step, self.clock = state, step, clock
        self.dir = folder(state)
        platforms.current.makedirs_private(self.dir)
        now = self.clock()
        self.st = {"step": step, "state": "running", "pid": os.getpid(), "started": now, "ts": now}
        self.arg = None   # main: the voice step's model
        self._shown = 0.0
        for leftover in (".in", ".cancel"):   # a code or a cancel meant for an earlier attempt
            self._drop(leftover)
        self.write()

    def write(self):
        self.st["ts"] = self.clock()
        catalog.write_private(status_path(self.state, self.step), self.st)

    def set(self, **kw):
        self.st.update(kw)
        self.write()

    def say(self, text):
        self.log(text)
        self.set(text=str(text)[-600:])

    def progress(self, done, total):
        now = self.clock()
        if done < total and now - self._shown < 0.5:
            return
        self._shown = now
        self.set(progress=[done, total])

    def log(self, text):
        try:
            os.makedirs(os.path.join(self.state, "logs"), exist_ok=True)
            body = "\n    ".join(x for x in redact.mask(str(text)).splitlines() if x.strip())
            with open(os.path.join(self.state, "logs", "setup.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {self.step}: {body}\n")
        except OSError:
            pass

    def finish(self, state, **kw):
        self.log(f"{state} {kw.get('why') or ''} {kw.get('text') or ''}".strip())
        self.st.pop("pid", None)
        self.set(state=state, **kw)
        for leftover in (".in", ".cancel"):
            self._drop(leftover)

    def _drop(self, ext):
        try:
            os.remove(os.path.join(self.dir, self.step + ext))
        except OSError:
            pass

    def cancelled(self):
        return os.path.exists(os.path.join(self.dir, self.step + ".cancel"))

    def take_input(self):
        """The text the person pasted for this step (once), or None."""
        path = os.path.join(self.dir, self.step + ".in")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read(4096)
        except OSError:
            return None
        self._drop(".in")
        return text.strip() or None


# --- running a program while the page talks to it --------------------------------------------------------------------

class Program:
    """A child with pipes: its output collected in the background, a line written to its input."""

    def __init__(self, argv, env, cwd=None):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  env=env, cwd=cwd, **platforms.current.apart())
        self.buf, self.lock = [], threading.Lock()
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self):
        for block in iter(lambda: self.p.stdout.read1(4096), b""):
            with self.lock:
                self.buf.append(block)

    def text(self):
        with self.lock:
            raw = b"".join(self.buf)
        return ANSI.sub("", raw.decode("utf-8", "replace"))

    def write(self, line):
        try:
            self.p.stdin.write(line.encode("utf-8") + b"\n")
            self.p.stdin.flush()
            return True
        except OSError:
            return False

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(5)
        self.reader.join(5)   # the rest of its output (the reason of a failure is in the last line)
        for pipe in (self.p.stdin, self.p.stdout):
            try:
                pipe.close()
            except OSError:
                pass

    def done(self):
        return self.p.poll() is not None


def tail(text, n=600):
    return redact.mask(" ".join(text.split()))[-n:]


def _wait(job, seconds, sleep):
    end = job.clock() + seconds
    while job.clock() < end:
        if job.cancelled():
            return False
        sleep(min(TICK, max(0.0, end - job.clock())) or TICK)
    return True


# --- the steps -------------------------------------------------------------------------------------------------------

def step_claude(job, installer=None):
    """The release's Claude Code into the hub's own folder (core/ownclaude: Anthropic's signed manifest, sha256, the
    file's own --version; the person's claude is not touched)."""
    from core import claudeinstall, ownclaude
    ver = ownclaude.release()
    if not ver:   # a working copy names no version: the hub runs claude from PATH
        path = ownclaude.command(job.state)[0]
        return job.finish("failed", why="release", text=i18n.t("cli.claude_setup.no_release", path=path))
    inst = installer or claudeinstall.Installer(job.state, say=job.say, progress=job.progress)
    try:
        path = ownclaude.fetch(job.state, ver, installer=inst)
    except claudeinstall.Failed as e:
        return job.finish("failed", why=e.why, text=str(e)[-800:])
    job.finish("done", version=ver, path=path, text=i18n.t("cli.claude_setup.done", version=ver, path=path))


def step_codex(job, installer=None):
    """Codex CLI from OpenAI's official releases (core/codexinstall: the pinned version, sha256 before it runs)."""
    from core import codexinstall
    inst = installer or codexinstall.Installer(job.state, say=job.say, progress=job.progress)
    try:
        got, path = inst.ensure_whole()
    except codexinstall.Failed as e:
        return job.finish("failed", why=e.why, text=str(e)[-800:])
    job.finish("done", version=got, path=path, text=i18n.t("cli.codex_setup.done", version=got, path=path))


def claude_cmd():
    from core.daemon import claude_command
    return claude_command()


def step_claude_upgrade(job):
    """The step a page of an earlier release asks for an older claude of npm or Homebrew: the hub no longer runs those
    managers — it takes its own copy (step_claude)."""
    return step_claude(job)


def browser_helper(job):
    """A program for BROWSER: it writes the link Claude Code wants opened into a file (the page opens it in the
    person's own browser). -> (program, file)."""
    out = os.path.join(job.dir, "login.browser")
    try:
        os.remove(out)
    except OSError:
        pass
    path, body = platforms.current.browser_helper(job.dir, out)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    os.chmod(path, 0o700)
    return path, out


def _link_from(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(16384)
    except OSError:
        return None
    m = CLAUDE_URL.search(text)
    return m.group(0) if m else None


def auth_status(claude, env=None):
    """`claude auth status` (JSON) -> {loggedIn, authMethod, email?, …} or None when it gives no JSON (an older Claude
    Code, a crash): unknown, not «signed out». The core asks the same before a new hub's spawn (core/claudeauth)."""
    from core import claudeauth
    return claudeauth.status(claude, env=env)


def step_login(job, claude=None, life=LOGIN_LIFE, sleep=time.sleep):
    """`claude auth login --claudeai` with pipes (no pty: installer S0-1). Two links: the one Claude Code prints
    (Anthropic shows a code, pasted back into the page — a phone, another computer) and the one it hands to BROWSER
    (returns to its own localhost — a browser on this machine signs in without pasting). Done is `claude auth status`."""
    claude = claude or claude_cmd()
    helper, out = browser_helper(job)
    env = dict(os.environ, BROWSER=helper)
    try:
        prog = Program(claude + ["auth", "login", "--claudeai"], env, cwd=os.path.expanduser("~"))
    except OSError as e:
        return job.finish("failed", why="start", text=str(e))
    end, sent = job.clock() + life, False
    try:
        while not prog.done():
            if job.cancelled():
                prog.stop()
                return job.finish("cancelled")
            if job.clock() > end:
                prog.stop()
                return job.finish("failed", why="timeout")
            seen = {}
            m = CLAUDE_URL.search(prog.text())
            if m and job.st.get("url") != m.group(0):
                seen["url"] = m.group(0)
            local = _link_from(out)
            if local and job.st.get("local_url") != local:
                seen["local_url"] = local
            if seen:
                job.set(**seen)
            code = job.take_input()
            if code:
                sent = prog.write(code)
                job.set(code_sent=True)
                job.log("a code was pasted")
            sleep(TICK)
    finally:
        prog.stop()
    said = prog.text()
    st = auth_status(claude)
    if prog.p.returncode == 0 and st and st.get("loggedIn"):
        return job.finish("done", account={k: st[k] for k in ("authMethod", "email", "orgName") if k in st})
    why = "code" if sent and re.search(r"status code 4\d\d|invalid", said, re.I) else "login"
    job.finish("failed", why=why, text=tail(said))


def step_chrome(job, pin=None, run=subprocess.run):
    from core import fetch
    from modules.setup import chrome
    pin = pin or chrome.pinned()
    if not pin:
        return job.finish("failed", why="platform", text=i18n.t("cli.setup.chrome_platform"))
    try:
        exe = chrome.install(job.state, pin, say=job.say, progress=job.progress, log=job.log, run=run)
    except (fetch.Failed, chrome.Refused, OSError, ValueError) as e:
        return job.finish("failed", why=getattr(e, "why", "unpack"), text=str(e)[-800:])
    job.finish("done", path=exe, version=pin["version"])


class Cancelled(Exception):
    """The person pressed «Cancel» during a download: what came so far stays (.part) for the next try."""


def step_voice(job, run=subprocess.run):
    """The built-in voice (modules/voice/install.py): the model `job.arg` of the hub language's rows (none — the
    recommended one), the engine when it is not in place, pinned by sha256, checked before the record names it. «Turn
    off» (install.mark_off) stops it between blocks or at the end and removes everything: off is off, whenever it was
    pressed. «Cancel» (.cancel) of a switch keeps the model in use and removes what came for the new one; of a first
    turn-on — removes what came, as «Turn off»."""
    from core import fetch
    from modules.voice import install, local
    if job.arg == install.CLOUD:
        return step_voice_cloud(job)
    had = local.installed(job.state) is not None
    if job.arg:
        job.set(model=job.arg)

    def off():   # .cancel, or «Turn off» / «Cancel» pressed before this process started (install.mark_off, mark_stop)
        return job.cancelled() or install.off_marked(job.state) or install.stop_marked(job.state)

    def undo():
        if install.off_marked(job.state) or not had:
            install.remove(job.state)
        else:
            install.prune(job.state)

    def progress(done, total):
        if off():
            raise Cancelled()
        job.progress(done, total)
    try:
        rec = install.install(job.state, i18n.current(), say=job.say, progress=progress, log=job.log, run=run,
                              cancelled=off, cfg=catalog.config_or_last(job.state) or {}, model=job.arg)
        if install.off_marked(job.state) or (off() and not had):   # pressed after the last check: the voice stays off
            raise Cancelled()
    except Cancelled:
        undo()
        return job.finish("cancelled")
    except (fetch.Failed, install.Refused, OSError, ValueError) as e:
        why = getattr(e, "why", None)
        if why == "cancelled":
            undo()
            return job.finish("cancelled")
        if why == "check" and had:   # the new model does not load: its file goes, the model in use stays
            install.prune(job.state)   # (a check stopped by a busy lock, a time-out or a kill — the files stay for «Again»)
        return job.finish("failed", why=why or "unpack", text=str(e)[-800:])
    if not rec:
        return job.finish("failed", why="check", text="")
    job.finish("done", version=rec["engine"], kind=rec["model"])


# the cloud row's refusal (modules/voice/chatgpt.CODES) -> the reason the page shows, as for a recording
CLOUD_WHY = {"cloud_login": "web.srv.voice_why_cloud_login", "cloud_limit": "web.srv.voice_why_cloud_limit",
             "cloud_failed": "web.srv.voice_why_cloud_failed"}


def step_voice_cloud(job):
    """The cloud row (modules/voice/chatgpt.py, spec d2140189): nothing is downloaded — the hub's Codex sign-in and the
    service answer one call on 1 s of silence, then the choice is written; a local model on disk stays as the fallback.
    The open flavor has no such row."""
    from core import cutout
    from modules.voice import install
    job.set(model=job.arg)
    if not cutout.present("voice_cloud"):
        return job.finish("failed", why="model", text=i18n.t("cli.setup.voice_no_model", model=job.arg))
    from modules.voice import chatgpt
    job.say(i18n.t("cli.setup.voice_checking"))
    why = chatgpt.check(job.state, install.base(i18n.current()))
    if job.cancelled() or install.off_marked(job.state) or install.stop_marked(job.state):
        return job.finish("cancelled")
    if why:
        return job.finish("failed", why=why, text=i18n.t(CLOUD_WHY.get(why, "web.srv.voice_why_cloud_failed")))
    chatgpt.choose(job.state)
    if install.off_marked(job.state):   # «Turn off» pressed during the check: the voice stays off
        install.remove(job.state)
        return job.finish("cancelled")
    job.finish("done", kind=install.CLOUD)


def step_github(job, gh=None, access=None, ensure=None, life=GITHUB_LIFE, sleep=time.sleep):
    """The hub's own gh (core/ghauth) by GitHub's device code: the page shows the code and opens github.com/login/device
    in the person's browser, where their password manager and 2FA work. Then the access step."""
    from core import ghauth
    gh = gh or ghauth.gh_program()
    if not gh:
        return job.finish("failed", why="no-gh", text=i18n.t("cli.setup.no_gh"))
    if not ghauth.signed_in(job.state):
        platforms.current.makedirs_private(ghauth.config_dir(job.state))
        try:
            prog = Program([gh, "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web",
                            "--insecure-storage"], ghauth.gh_env(job.state), cwd=job.state)
        except OSError as e:
            return job.finish("failed", why="start", text=str(e))
        prog.write("")   # «Press Enter to open github.com» when it thinks there is a terminal
        end = job.clock() + life
        try:
            while not prog.done():
                if job.cancelled():
                    prog.stop()
                    return job.finish("cancelled")
                if job.clock() > end:
                    prog.stop()
                    return job.finish("failed", why="timeout")
                m = DEVICE_CODE.search(prog.text())
                if m and job.st.get("code") != m.group(1):
                    job.set(code=m.group(1), verify=DEVICE_URL)
                sleep(TICK)
        finally:
            prog.stop()
        if prog.p.returncode != 0 or not ghauth.signed_in(job.state):
            return job.finish("failed", why="github", text=tail(prog.text()))
        job.st.pop("code", None)
        job.log(f"signed in to GitHub as {ghauth.login(job.state) or '?'}")
    job.set(login=ghauth.login(job.state), signed=True)
    return check_access(job, access=access, ensure=ensure, sleep=sleep)


def step_access(job, access=None, ensure=None, sleep=time.sleep):
    return check_access(job, access=access, ensure=ensure, sleep=sleep)


def check_access(job, access=None, ensure=None, life=INVITE_LIFE, sleep=time.sleep):
    """git access to the team's modules the way the hub installs them (modules/update/private: the same address,
    codestore's environment — the hub's GitHub sign-in included). No access: the invitation page, checked again every
    INVITE_EVERY seconds; access: the modules are installed now (the existing ensure step)."""
    from modules.update import private
    pin = private.load_pin(REPO)
    if not pin:
        return job.finish("done", access="none")
    cfg = catalog.config_or_last(job.state)
    url = private.address(pin, private.origin_of(REPO, cfg))
    access = access or private.ls_remote
    end = job.clock() + life
    while True:
        result, _, why = access(url, pin["ref"])
        if result == "ok":
            break
        if result != "no-access":
            return job.finish("failed", why=result, text=str(why)[-400:])
        from core import gitaccess
        job.set(access="no-access", invite=f"https://github.com/{pin['repo']}/invitations",
                kind=gitaccess.kind(why) or "", text=str(why)[-300:])
        if job.clock() >= end:
            return job.finish("failed", why="invite", access="no-access")
        if not _wait(job, INVITE_EVERY, sleep):
            return job.finish("cancelled")
    job.set(access="ok", text=i18n.t("cli.setup.modules_installing"))
    try:
        got = (ensure or (lambda: private.Ensure(REPO, job.state, cfg).run()))()
    except Exception as e:  # noqa: BLE001 — access is there; the hourly step of the update installs them later
        job.log(f"ensure: {e!r}")
        got = None
    job.finish("done", access="ok", modules=(got or {}).get("state") if isinstance(got, dict) else None)


DOCTOR_LINE = re.compile(r"^(ok|warn|fail)\s+(.+?)(?:\s{2,}(.*))?$")


def step_doctor(job, run=subprocess.run):
    """`hub doctor` of this hub: its rows for the page."""
    try:
        r = run([sys.executable, os.path.join(REPO, "bin", "hub"), "doctor"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return job.finish("failed", why="start", text=str(e))
    rows = []
    for line in (r.stdout or "").splitlines():
        m = DOCTOR_LINE.match(line.rstrip())
        if m:
            rows.append([m.group(1), m.group(2).strip(), redact.mask((m.group(3) or "").strip())[:400]])
    job.finish("done" if r.returncode == 0 else "failed", rows=rows[:200],
               **({} if rows else {"text": tail(r.stderr or r.stdout or "")}))


RUN = {"claude": step_claude, "claude_upgrade": step_claude_upgrade, "codex": step_codex, "login": step_login,
       "chrome": step_chrome,
       "github": step_github, "access": step_access, "doctor": step_doctor, "voice": step_voice}


def main(argv):
    """job.py <state> <step> [<arg>] — arg: the voice step's model (taken with the step's lock: of two starts the one
    that runs is the one whose model the page then shows)."""
    if len(argv) not in (2, 3) or argv[1] not in STEPS or (len(argv) == 3 and (argv[1] != "voice" or not ARG.fullmatch(argv[2]))):
        print("job.py <state> " + "|".join(STEPS) + " [<model>]", file=sys.stderr)
        return 2
    state, step = argv[:2]
    platforms.current.makedirs_private(folder(state))
    with open(os.path.join(folder(state), f"{step}.lock"), "a+") as lock:
        try:
            platforms.current.try_lock(lock)
        except OSError:
            return 0   # the same step is running: the page shows that one
        job = Job(state, step)
        job.arg = argv[2] if len(argv) == 3 else None
        try:
            RUN[step](job)
        except Exception as e:  # noqa: BLE001 — the page must see the step ended, not «running» forever
            job.finish("failed", why="crash", text=f"{type(e).__name__}: {e}"[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
