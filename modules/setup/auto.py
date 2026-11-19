#!/usr/bin/env python3
"""The app's start without a wizard (app spec §4a.1; the owner 2026-11-07: "the app can do all of this by itself at its
start… a setup wizard is an extra item"). hub-up starts `auto.py <state folder>` at every start of an app hub; it runs
the steps of «Setup» in order, each in its own process (job.py, as a button of the page would), and ends when nothing
is left for it:

- Claude Code (step claude) and Chrome for the agents (chrome) at once. Claude Code: the release's own copy not in the
  hub's folder yet — downloaded there (core/ownclaude; whatever claude the person has, from whatever installer, is not
  touched); a code that names no version (a working copy) — nothing: it runs claude from PATH. Chrome: when its pin
  has a build for this machine and the system lets it start;
- the sign-in (login) once Claude Code is there and the hub has none — neither its own nor through conduit or the
  team's pool (setup.item_login): the step waits for the person with its link, the band shows that link as one button;
- `hub doctor` last, when Claude Code and Chrome are no longer under way and the last check is older than DOCTOR_FRESH.
A step this run started that failed for the network starts again by itself after a pause (WAIT, doubling up to
WAIT_MAX; auto.json names when — the band says "no network, the hub will try again"); any other failure is the band's,
with "Retry". Every step starts once per run: a failure that is not the network waits for the person, not for a loop.
When the hub turns ready during the run (it was not at its start), the owner gets one line: "The hub is ready".
One auto.py per state folder (a lock); its log lines go to hub-up's log."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import platforms  # noqa: E402
from core import catalog  # noqa: E402

TICK = 2.0
WAIT = 30          # s: the first pause before a step that failed for the network starts again
WAIT_MAX = 600     # s: the longest pause
LIFE = 24 * 3600   # s: the run gives up waiting for the network after this; the next start of the hub begins anew
FILE = "auto.json"
WORKING = 7 * 86400   # s: as the page's Web.WORKING
# what core/fetch says when the network is not there: no answer at all ("network") or a download that broke off after
# its own tries ("broken") — both pass by themselves once the network is back
NETWORK = ("network", "broken")


class Auto:
    """setup — modules.setup.setup.Setup of an app hub; tell(text) — the line for the owner; clock and sleep — tests."""

    def __init__(self, state, setup, tell=None, clock=time.time, sleep=time.sleep, working=None):
        self.state, self.setup, self.clock, self.sleep = state, setup, clock, sleep
        # sessions of this hub answered lately: their sign-in works (a key, a gateway) whatever `claude auth status`
        # says — no sign-in is started for it, as the page's band never asks such a hub (setup.item_login)
        self.working = answered(state, clock()) if working is None else working
        self.tell = tell or (lambda text: None)
        self.started, self.just, self.waits, self.pause = set(), set(), {}, {}
        self.began, self.was_ready, self.written, self.redo = clock(), None, None, False

    def start(self, step):
        self.just.add(step)   # its file says "running" only from the next look
        self.started.add(step)
        self.waits.pop(step, None)
        print(f"setup: {step}", flush=True)
        self.setup.run(step)

    def due(self, step, item):
        """Start the step now: never started in this run; or it failed for the network and its pause is over."""
        j = (item or {}).get("job") or {}
        if j.get("state") == "running":
            return False
        if step not in self.started:
            return True
        if j.get("state") != "failed" or j.get("why") not in NETWORK:
            return False
        at = self.waits.get(step)
        if at is None:
            pause = self.pause[step] = min(WAIT_MAX, self.pause.get(step, WAIT / 2) * 2)
            self.waits[step] = at = self.clock() + pause
        return self.clock() >= at

    def round(self):
        """One look and what to start. -> True while something is still under way or waits to start again."""
        self.just = set()
        st = self.setup.status(full=True, working=self.working)
        items = {i["id"]: i for i in st["items"]}
        turned = False
        if self.was_ready is None:
            self.was_ready = st["ready"]
        elif st["ready"] and not self.was_ready:
            self.was_ready = turned = True
            self.tell("ready")
        if "codex" in items:   # a hub on Codex (the person chose it on the band): Codex CLI, then its sign-in
            program, login, sign_in = "codex", "codex_login", self.setup.codex_sign_in
        else:
            program, login, sign_in = "claude", "login", lambda: self.setup.run("login")
        p = items.get(program) or {}
        # Claude Code: the release's own copy is the hub's to download whatever else is there (pending: the hub runs
        # another claude until then); doctor waits for it. Codex CLI: none at all, or the hub's ~/.local/bin copy that is
        # older; a place it cannot tell, a program whose --version stumbled — the band's button (never a second install)
        if program == "claude":   # nothing else is installed: a code that names no version runs what PATH has
            mine = p.get("pending") and p.get("state") != "running"
        else:
            mine = p.get("state") in ("todo", "failed") and (not p.get("version") and not p.get("found") or p.get("own"))
        if mine and self.due(program, p):
            self.start(program)
        busy = p.get("state") == "running" or (p.get("job") or {}).get("state") == "running" or program in self.waits \
            or program in self.just
        lg = items.get(login) or {}
        if p.get("state") == "ok" and lg.get("state") in ("todo", "failed") and login not in self.started:
            self.started.add(login)
            self.just.add(login)
            print(f"setup: {login}", flush=True)
            sign_in()   # waits for the person (a link, a device code): the band's one button
        waiting = login in self.just or lg.get("state") == "running" and login in self.started
        self.redo = self.redo or turned
        busy = self.chrome_and_doctor(items, busy or waiting) or busy or waiting
        self.write()
        return busy

    def chrome_and_doctor(self, items, busy):
        """Chrome for the agents while the rest goes on; `hub doctor` once nothing else is under way and the last check is
        old — and once more when the hub has just turned ready (the first one saw it before its sign-in)."""
        ch = items.get("chrome") or {}
        wanted = ch.get("state") in ("todo", "failed") and ch.get("available") and "sandbox" not in ch
        if wanted and self.due("chrome", ch):
            self.start("chrome")
        busy |= ch.get("state") == "running" or "chrome" in self.waits or "chrome" in self.just
        doc = items.get("doctor") or {}
        if not busy and doc.get("state") != "running" and (self.redo or doc.get("stale") and "doctor" not in self.started):
            self.redo = False
            self.start("doctor")
            busy = True
        return busy or doc.get("state") == "running"

    def write(self):
        data = {"pid": os.getpid(), "started": self.began, "waits": {k: round(v) for k, v in self.waits.items()}}
        if data != self.written:
            self.written = data
            platforms.current.makedirs_private(os.path.join(self.state, "setup"))
            catalog.write_private(os.path.join(self.state, "setup", FILE), data)

    def run(self):
        while self.clock() - self.began < LIFE:
            try:
                more = self.round()
            except Exception as e:  # noqa: BLE001 — a probe that failed once: the next round looks again
                print(f"setup: the round failed: {type(e).__name__}: {e}", flush=True)
                more = True
            if not more:
                return 0
            self.sleep(TICK)
        return 0


def answered(state, now, within=WORKING):
    """A session of the hub answered within `within` seconds (state.json, the core's checkpoint: what the page reads as
    out_head_ts in the snapshot)."""
    from core import journal
    try:
        st = journal.read_state_file(state)   # read whole first: Windows refuses hubd's replace while a reader holds it
    except (OSError, ValueError):
        return False
    sessions = ((st.get("state") or {}) if isinstance(st, dict) else {}).get("sessions") or {}
    return isinstance(sessions, dict) and any(isinstance(x, dict) and isinstance(x.get("out_head_ts"), (int, float))
                                              and x["out_head_ts"] > now - within for x in sessions.values())


def tell_owner(state):
    """"The hub is ready" for the owner: one notification with a constant id (a second run's replaces it)."""
    def tell(what):
        from core import i18n, spool
        spool.put(os.path.join(state, "spool"), "owner.notify", None,
                  {"text": i18n.t("core.setup.ready"), "title": i18n.t("core.setup.ready_title"), "id": "setup-ready",
                   "source": "setup", "channel": "cli", "owner": True}, src="setup")
    return tell


def main(argv):
    if len(argv) != 1:
        print("auto.py <state>", file=sys.stderr)
        return 2
    state = argv[0]
    from modules.setup import job, setup
    platforms.current.makedirs_private(job.folder(state))
    with open(os.path.join(job.folder(state), "auto.lock"), "a+") as lock:
        try:
            platforms.current.try_lock(lock)
        except OSError:
            return 0   # another run of this hub is on it
        s = setup.Setup(state, lambda: catalog.config_or_last(state))
        if not s.app:
            return 0   # a hub from git: nothing runs by itself (the band has the buttons)
        return Auto(state, s, tell=tell_owner(state)).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
