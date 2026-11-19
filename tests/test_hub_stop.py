"""`hub stop`: a soft stop of a hub started by hub-up (2026-10-05). Under an OS service — a refusal with its command: the
service would bring the hub up again (review of PR #18: the launchd agent has KeepAlive — the hub came back after ~10 s,
while the command said it would stop it)."""
import tests  # noqa: F401 — do not see the machine's settings
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from core import i18n
from platforms import posix
from tests import waiting
from tests.fakes.procs import alive, kill

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB_UP, HUB = (os.path.join(REPO, "bin", n) for n in ("hub-up", "hub"))
PART = ("import os, sys, time\n"
        "open(sys.argv[1], 'a').write(f'{sys.argv[2]} {os.getpid()} {os.environ.get(\"HUB_UP_CHILD\", \"-\")} '\n"
        "f'{os.environ.get(\"HUB_TREE\", \"-\")}\\n')\n"
        "time.sleep(120)\n")
# a part that on SIGTERM records whether the `hub stop` file still exists (how the core learns the hub is leaving for good)
SEES_STOP = ("import os, signal, sys, time\n"
             "def bye(*_):\n"
             "    open(sys.argv[1] + '.stop', 'a').write(str(os.path.exists(os.path.join(os.environ['HUB_STATE_DIR'], 'stop'))))\n"
             "    sys.exit(0)\n"
             "signal.signal(signal.SIGTERM, bye)\n"   # before the journal line: the test counts the part as up by it
             "open(sys.argv[1], 'a').write(f'{sys.argv[2]} {os.getpid()} - -\\n')\n"
             "time.sleep(120)\n")
# a part that records whether it got the tests' lock wait of hub-up (bin/hub-up lock_wait)
SEES_LOCK_WAIT = ("import os, sys\n"
                  "open(sys.argv[1] + '.wait', 'a').write(os.environ.get('HUB_UP_LOCK_WAIT', '-'))\n" + PART)


# `hub stop` (the file in argv[1]) run with its sleeps counted: the last line of stderr is "SLEEPS <n>"
COUNT_SLEEPS = ("import runpy, sys, time\n"
                "n, real = [0], time.sleep\n"
                "def counted(seconds):\n"
                "    n[0] += 1\n"
                "    real(seconds)\n"
                "time.sleep = counted\n"
                "path = sys.argv[1]\n"
                "sys.argv = [path, 'stop']\n"
                "try:\n"
                "    runpy.run_path(path, run_name='__main__')\n"
                "finally:\n"
                "    sys.stderr.write('SLEEPS %d\\n' % n[0])\n")


def text_of(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def until(pred, timeout=120.0):   # a fuse, not a limit: the wait ends at once when the condition holds
    return waiting.until(pred, timeout, 0.1)


# a second hub-up over a live one: the lock is held for the whole test, so it need not wait LOCK_WAIT to see that
SECOND = {"HUB_UP_LOCK_WAIT": "0"}


class HubStop(unittest.TestCase):
    def start(self, service=None, fresh=False, stop_file=False, restarted=False, tree=None, part_code=PART, extra=None):
        self.state = tempfile.mkdtemp(prefix="hub-stop-")
        self.log = os.path.join(self.state, "parts.log")
        self.seen = {}   # pid -> when it was first read from the log (procs.kill: the process started by then)
        if fresh:  # the state directory does not exist yet — the first start
            self.state = os.path.join(self.state, "state")
        part = os.path.join(os.path.dirname(self.log), "part.py")
        with open(part, "w") as f:
            f.write(part_code)
        # the service is only its own: a runner under systemd or launchd must not make hub-up a "service"
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SERVICE", "INVOCATION_ID", "XPC_SERVICE_NAME")}
        # parts are not the core: how hub-up stops the core is checked by tests/test_stop.py
        env.update(HUB_STATE_DIR=self.state, HUB_UP_PARTS=json.dumps(
            {"web": [part, self.log, "web"], "files": [part, self.log, "files"]}))
        if service:
            env["HUB_SERVICE"] = service
        if restarted:  # as after a full restart (core/restart.RESTARTED)
            env["HUB_UP_RESTARTED"] = "1"
        if tree:
            env["HUB_TREE"] = tree
        env.update(extra or {})
        if stop_file:
            open(os.path.join(self.state, "stop"), "w").close()
        self.p = subprocess.Popen([sys.executable, HUB_UP], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.cleanup)
        self.assertTrue(until(lambda: len(self.pids()) == 2), "the parts did not come up")
        return env

    def cleanup(self):
        if self.p.poll() is None:
            self.p.terminate()
            self.p.wait(120)
        for pid in self.pids():
            kill(pid, self.seen[pid])

    def pids(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            pids = [int(x.split()[1]) for x in f.read().splitlines() if x]
        at = time.time()   # after the read: every process read started by then
        for pid in pids:
            self.seen.setdefault(pid, at)
        return pids

    def stop(self, env):
        # hub writes UTF-8 under any locale (platforms.utf8_mode): read it the same way, not as cp1251
        return subprocess.run([sys.executable, HUB, "stop"], env=env, capture_output=True, text=True, encoding="utf-8",
                              timeout=120)

    def test_stops_hub_up_and_its_parts(self):
        env = self.start()
        out = self.stop(env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.p.wait(120), 0)
        self.assertTrue(until(lambda: not any(alive(pid) for pid in self.pids())))
        self.assertFalse(os.path.exists(os.path.join(self.state, "hub-up.json")))

    def test_parts_know_they_are_children_of_hub_up(self):
        # the HUB_UP_CHILD mark on hub-up descendants — their hub-up is not a service (platforms.service)
        self.start()
        with open(self.log) as f:
            self.assertEqual({x.split()[2] for x in f.read().splitlines() if x}, {"1"})

    def test_parts_do_not_carry_the_tree_of_the_session_that_started_hub_up(self):
        # a hub-up started from a session is not its background command: the cleanup after a departed session (hosts/common.Trees)
        # would kill the core and the parts carrying its mark
        self.start(tree="maps@/home/x/.hub-core")
        with open(self.log) as f:
            self.assertEqual({x.split()[3] for x in f.read().splitlines() if x}, {"-"})

    @unittest.skipIf(os.name == "nt", "POSIX: the parts are stopped by a signal")
    def test_parts_see_the_stop_file_while_they_stop(self):
        # the core kills sessions' background commands only on `hub stop` — by the stop file (daemon.stop_sessions); hub-up used to remove
        # it before stopping the children, so the core always saw a restart (review 2026-10-16)
        env = self.start(part_code=SEES_STOP)
        self.assertEqual(self.stop(env).returncode, 0)
        self.assertEqual(self.p.wait(120), 0)
        self.assertEqual(text_of(self.log + ".stop"), "TrueTrue")
        self.assertFalse(os.path.exists(os.path.join(self.state, "stop")))

    def test_first_start_creates_the_state_dir(self):
        # review of PR #18: hub-up wrote hub-up.json into a directory that does not exist yet (only hubd created it) — and crashed
        env = self.start(fresh=True)
        self.assertEqual(self.stop(env).returncode, 0)
        self.assertEqual(self.p.wait(120), 0)

    def test_stop_during_a_full_restart_is_kept(self):
        # review of PR #18: `hub stop` between stopping the children and starting the new image — the new image erased the `stop` file
        # as a stale one, and the hub came back although the command said it would stop it
        self.start(stop_file=True, restarted=True)
        self.assertEqual(self.p.wait(120), 0)

    def test_a_stale_stop_file_does_not_stop_a_fresh_start(self):
        self.start(stop_file=True)
        time.sleep(2)
        self.assertIsNone(self.p.poll())

    def test_a_stopping_hub_up_keeps_a_newer_hub_ups_file(self):
        # review of PR #18: the old hub-up, after waiting for the children, erased the new one's hub-up.json — `hub stop` no longer saw it
        self.start()
        up = os.path.join(self.state, "hub-up.json")
        with open(up, "w") as f:
            json.dump({"pid": 999999, "service": None}, f)
        open(os.path.join(self.state, "stop"), "w").close()
        self.assertEqual(self.p.wait(120), 0)
        with open(up) as f:
            self.assertEqual(json.load(f)["pid"], 999999)

    def test_a_second_hub_up_leaves_at_once(self):
        """The Windows scheduler starts hub-up again every few minutes — the death of the whole python tree (a session
        ran `taskkill /IM python.exe`) no longer leaves the hub down. The hub is already running — a second hub-up exits
        at once, starting nothing and not touching the first one's hub-up.json."""
        env = self.start()
        up = os.path.join(self.state, "hub-up.json")
        with open(up) as f:
            first = json.load(f)
        out = subprocess.run([sys.executable, HUB_UP], env={**env, **SECOND}, capture_output=True, text=True,
                             encoding="utf-8", timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("already running", out.stdout)
        self.assertEqual(len(self.pids()), 2)
        with open(up) as f:
            self.assertEqual(json.load(f), first)
        self.assertIsNone(self.p.poll())

    def test_the_parts_do_not_get_the_tests_lock_wait(self):
        # hub-up takes it out of its own environment, which the parts get (and the image a POSIX full restart execs —
        # that path is not run here; a restart under the Windows supervisor: LockWait.test_a_restart_keeps_the_whole_wait)
        self.start(part_code=SEES_LOCK_WAIT, extra=SECOND)
        self.assertEqual(text_of(self.log + ".wait"), "--")

    def test_a_second_hub_up_writes_into_a_log_someone_else_holds(self):
        """--log: hub-up appends to the log itself, which others hold open for writing (parts of a live hub or what
        survived a killed hub) — the cmd `>>` redirection did not start hub-up at all in that case (2026-10-20)."""
        env = self.start()
        log = os.path.join(self.state, "logs", "hub-up.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as held:
            held.write("before\n")
            held.flush()
            out = subprocess.run([sys.executable, HUB_UP, "--log", log], env={**env, **SECOND}, capture_output=True,
                                 text=True, encoding="utf-8", timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "")
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("before\n"), text)
        self.assertIn("already running", text)

    def test_a_log_that_does_not_open_does_not_stop_hub_up(self):
        """The log cannot be opened (held without shared access by what survived a killed hub; here — a directory in its place):
        hub-up still works and writes to a fallback file next to it instead of crashing."""
        env = self.start()
        log = os.path.join(self.state, "logs", "hub-up.log")
        os.makedirs(log)
        out = subprocess.run([sys.executable, HUB_UP, "--log", log], env={**env, **SECOND}, capture_output=True,
                             text=True, encoding="utf-8", timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(os.path.join(self.state, "logs", "hub-up.2.log"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("does not open", text)
        self.assertIn("already running", text)

    def scheduled(self, env):
        """hub-up the way a Windows scheduler task starts it (at logon and repeated every 5 min)."""
        p = subprocess.Popen([sys.executable, HUB_UP, "--scheduled"], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.addCleanup(lambda: p.poll() is None and (p.terminate(), p.wait(120)))
        return p

    def test_hub_stop_keeps_the_hub_down_for_the_scheduler(self):
        """The scheduler task's 5-minute repeat brought the hub up after `hub stop` / `hub off` (review of PR #190): the owner's
        stop leaves a mark, and a hub-up from the scheduler exits at once, quietly and with 0 when it is there."""
        env = self.start()
        self.assertEqual(self.stop(env).returncode, 0)
        self.assertEqual(self.p.wait(120), 0)
        self.assertTrue(os.path.exists(os.path.join(self.state, "stopped")))
        up = self.scheduled(env)
        try:
            out, err = up.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            self.fail("hub-up started by the scheduler brought the hub up after hub stop")
        self.assertEqual((up.returncode, out, err), (0, "", ""))
        self.assertEqual(len(self.pids()), 2)   # the parts were not started
        self.assertTrue(os.path.exists(os.path.join(self.state, "stopped")))

    def test_hub_stop_of_a_dead_hub_still_keeps_it_down(self):
        # review: the hub is killed entirely, the owner does hub stop / hub off so it does not come back — there is no hub-up, but the mark is needed:
        # otherwise the scheduler repeat would bring the hub up in 5 minutes
        env = self.start()
        self.p.kill()
        self.p.wait(120)
        for pid in self.pids():
            kill(pid, self.seen[pid])
        os.unlink(os.path.join(self.state, "hub-up.json"))
        self.assertNotEqual(self.stop(env).returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.state, "stopped")))

    def test_the_scheduler_over_a_hand_started_hub_leaves_silently(self):
        # the hub was started by hand (the task is not running, IgnoreNew does not stop the repeat): the 5-minute repeat exits silently, the log does not grow
        env = self.start()
        out = subprocess.run([sys.executable, HUB_UP, "--scheduled"], env={**env, **SECOND}, capture_output=True,
                             text=True, encoding="utf-8", timeout=120)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, "", ""))
        self.assertEqual(len(self.pids()), 2)

    def test_a_manual_hub_up_lifts_the_stop(self):
        # hub-up from a terminal — the owner starts the hub again: it clears the mark, the next scheduler repeat does not see it
        env = self.start()
        self.assertEqual(self.stop(env).returncode, 0)
        self.assertEqual(self.p.wait(120), 0)
        os.unlink(self.log)
        self.p = subprocess.Popen([sys.executable, HUB_UP], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(until(lambda: len(self.pids()) == 2), "the parts did not come up")
        self.assertFalse(os.path.exists(os.path.join(self.state, "stopped")))

    def test_a_crash_leaves_no_stop_and_the_scheduler_brings_the_hub_back(self):
        # the hub was killed (not hub stop) — no mark, the scheduler repeat brings it up, as before
        env = self.start()
        self.p.kill()
        self.p.wait(120)
        for pid in self.pids():
            kill(pid, self.seen[pid])
        os.unlink(self.log)
        self.assertFalse(os.path.exists(os.path.join(self.state, "stopped")))
        self.scheduled(env)
        self.assertTrue(until(lambda: len(self.pids()) == 2), "the scheduler retry did not bring up the killed hub")

    def test_hub_on_lifts_the_stop(self):
        """`hub on` on Windows starts the same scheduler task (hub-up --scheduled): it clears the mark before that."""
        state = tempfile.mkdtemp(prefix="hub-on-")
        open(os.path.join(state, "stopped"), "w").close()
        loader = importlib.machinery.SourceFileLoader("hub_cli", HUB)
        hub = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_cli", loader))
        loader.exec_module(hub)
        hub.STATE, hub.ME, hub.OUTSIDER = state, None, None
        seen = []
        task = {"kind": "task", "id": "Agents Hub", "path": None, "port": None}
        with mock.patch.object(hub.platforms.current, "service_unit", return_value=task), \
                mock.patch.object(hub.platforms.current, "service_commands",
                                  return_value={"on": ["schtasks", "/Run"], "off": None, "status": None}), \
                mock.patch.object(hub.subprocess, "run", side_effect=lambda cmd, **_: (
                    seen.append(os.path.exists(os.path.join(state, "stopped"))),
                    subprocess.CompletedProcess(cmd, 0, "", ""))[1]):
            hub.service_cmd("on")
        self.assertEqual(seen, [False])

    def test_hub_off_on_windows_says_the_hub_stays_off(self):
        """`hub off` on Windows is a soft stop: the stop mark keeps the logon task from bringing the hub back."""
        loader = importlib.machinery.SourceFileLoader("hub_cli", HUB)
        hub = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_cli", loader))
        loader.exec_module(hub)
        hub.STATE, hub.ME, hub.OUTSIDER = tempfile.mkdtemp(prefix="hub-off-"), None, None
        task = {"kind": "task", "id": "Agents Hub", "path": None, "port": None}
        out, stops = io.StringIO(), []
        with mock.patch.object(hub.platforms.current, "service_unit", return_value=task), \
                mock.patch.object(hub.platforms.current, "service_commands",
                                  return_value={"on": ["schtasks", "/Run"], "off": None, "status": None}), \
                mock.patch.object(hub, "main", side_effect=stops.append), contextlib.redirect_stdout(out):
            hub.service_cmd("off")
        self.assertEqual(stops, [["stop"]])
        self.assertIn(i18n.t("cli.service.stays_off"), out.getvalue())

    def test_hub_off_under_a_service_says_what_brings_the_hub_back(self):
        """launchd loads the agent again at the next sign-in; systemd starts the unit at the next boot when the user's
        manager lingers (the installer asks for it), and at the next sign-in when it does not."""
        loader = importlib.machinery.SourceFileLoader("hub_cli", HUB)
        hub = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_cli", loader))
        loader.exec_module(hub)
        hub.STATE, hub.ME, hub.OUTSIDER = tempfile.mkdtemp(prefix="hub-off-"), None, None
        for kind, lingers, key in (("launchd", True, "cli.service.stays_off_login"),
                                   ("systemd", True, "cli.service.stays_off_boot"),
                                   ("systemd", False, "cli.service.stays_off_login")):
            unit = {"kind": kind, "id": "agents-hub", "path": None, "port": None}
            up = iter([0, 0, 1])   # status: up; the stop; status: down
            out = io.StringIO()
            with mock.patch.object(hub.platforms.current, "service_unit", return_value=unit), \
                    mock.patch.object(hub.platforms.current, "service_commands",
                                      return_value={"on": ["on"], "off": ["off"], "status": ["status"]}), \
                    mock.patch.object(hub.platforms.current, "lingers", return_value=lingers, create=True), \
                    mock.patch.object(hub.subprocess, "run", lambda *a, **k: mock.Mock(returncode=next(up))), \
                    contextlib.redirect_stdout(out):
                hub.service_cmd("off")
            self.assertIn(i18n.t(key), out.getvalue(), (kind, lingers))
            self.assertEqual(out.getvalue().count("\n"), 2, out.getvalue())   # the line "off" and this one

    def test_a_session_may_not_stop_the_hub(self):
        env = {**self.start(), "HUB_SESSION": "worker"}
        self.assertNotEqual(self.stop(env).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "stop")))

    def test_refuses_a_hub_held_by_a_service(self):
        cmd = "launchctl bootout gui/$(id -u)/com.hub-test"
        env = self.start(service=cmd)
        stop = os.path.join(self.state, "stop")
        out = self.stop(env)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(cmd, out.stderr)
        self.assertFalse(os.path.exists(stop))
        open(stop, "w").close()  # and hub-up removes the stop file bypassing the command, but does not stop
        self.assertTrue(until(lambda: not os.path.exists(stop), 120))
        time.sleep(1)
        self.assertIsNone(self.p.poll())

    def test_children_do_not_inherit_the_service(self):
        # review of PR #18: HUB_SERVICE from the launchd agent reached the parts and sessions — their hub-up took itself for a service
        probe = os.path.join(tempfile.mkdtemp(prefix="svc-env-"), "probe.py")
        seen = probe + ".out"
        with open(probe, "w") as f:
            f.write(f"import os, time\nopen({seen!r}, 'w').write(os.environ.get('HUB_SERVICE', '-'))\ntime.sleep(120)\n")
        state = tempfile.mkdtemp(prefix="hub-stop-svc-")
        env = {k: v for k, v in os.environ.items() if k not in ("INVOCATION_ID", "XPC_SERVICE_NAME")}
        env.update(HUB_STATE_DIR=state, HUB_SERVICE="test service", HUB_UP_PARTS=json.dumps({"web": [probe]}))
        p = subprocess.Popen([sys.executable, HUB_UP], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.poll() is None and (p.terminate(), p.wait(120))))
        self.assertTrue(until(lambda: os.path.exists(seen) and text_of(seen)))
        self.assertEqual(text_of(seen), "-")
        with open(os.path.join(state, "hub-up.json")) as f:
            up = json.load(f)
        self.assertEqual(up["service"], "test service")
        # and its start time: the update watchdog (a copy of the previous release) recognises this very hub-up
        from modules.update import trial
        self.assertIsNotNone(up["started"])
        self.assertTrue(trial.is_hub_up(up["pid"], up["started"]))

    def test_a_plugin_may_not_stop_the_hub(self):
        # review of PR #18: an extension process (HUB_EXTENSION, no HUB_SESSION) stopped the hub — a full `hub restart`
        # is closed to it, while stop was open
        env = {**self.start(), "HUB_EXTENSION": "hello"}
        self.assertNotEqual(self.stop(env).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "stop")))

    def test_a_reused_pid_is_not_hub_up(self):
        # hub-up crashed, its pid was taken by a foreign process — `hub stop` must not report "will stop"
        state = tempfile.mkdtemp(prefix="hub-stop-reused-")
        with open(os.path.join(state, "hub-up.json"), "w") as f:
            json.dump({"pid": os.getpid(), "service": None}, f)
        out = subprocess.run([sys.executable, HUB, "stop"], env={**os.environ, "HUB_STATE_DIR": state},
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("hub-up is not running", out.stderr)

    @unittest.skipIf(os.name == "nt", "on Windows a full restart is a new process: hub stop waits for its record")
    def test_a_stale_file_is_answered_at_once(self):
        # review: waiting for the new hub-up is needed only on Windows — on POSIX exec keeps the pid and hub-up.json
        state = tempfile.mkdtemp(prefix="hub-stop-stale-")
        with open(os.path.join(state, "hub-up.json"), "w") as f:
            json.dump({"pid": 999999, "service": None}, f)
        # at once = the command waits for nothing. Not by the clock: on a loaded machine the start of the command alone is
        # seconds; the waits of the command are its sleeps, so they are counted
        out = subprocess.run([sys.executable, "-c", COUNT_SLEEPS, HUB], env={**os.environ, "HUB_STATE_DIR": state},
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertIn("hub-up is not running", out.stderr)
        self.assertIn("SLEEPS 0", out.stderr)

    def test_without_hub_up_says_so(self):
        # there is no hub-up (the hub is not running): nobody to read the stop file
        state = tempfile.mkdtemp(prefix="hub-stop-none-")
        out = subprocess.run([sys.executable, HUB, "stop"], env={**os.environ, "HUB_STATE_DIR": state},
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("hub-up is not running", out.stderr)
        self.assertFalse(os.path.exists(os.path.join(state, "stop")))


@unittest.skipIf(os.name == "nt", "an OS service on POSIX: os.getuid")
class PosixService(unittest.TestCase):
    def service(self, env, platform="darwin", cgroup=None, parent=1, grandparent=None):
        home = tempfile.mkdtemp(prefix="svc-home-")
        os.makedirs(os.path.join(home, "Library", "LaunchAgents"))
        open(os.path.join(home, "Library", "LaunchAgents", "com.hub-core.plist"), "w").close()
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("XPC_SERVICE_NAME", "INVOCATION_ID", "SYSTEMD_EXEC_PID", "HUB_UP_CHILD")}
        with mock.patch.dict(os.environ, {**clean, "HOME": home, **env}, clear=True), \
                mock.patch.object(posix.sys, "platform", platform), \
                mock.patch.object(posix.os, "getppid", return_value=parent), \
                mock.patch.object(posix, "_parent_of", return_value=grandparent):
            if cgroup is None:
                return posix.service()
            with mock.patch("builtins.open", mock.mock_open(read_data=cgroup)):
                return posix.service()

    def test_launchd_agent_is_known_by_its_label(self):
        self.assertEqual(self.service({"XPC_SERVICE_NAME": "com.hub-core"}),
                         f"launchctl bootout gui/{os.getuid()}/com.hub-core")

    def test_launchd_agent_through_the_app_launcher(self):
        self.assertIsNotNone(self.service({"XPC_SERVICE_NAME": "com.hub-core"}, parent=4242, grandparent=1))

    def test_terminal_is_not_a_service(self):
        for label in ("0", "application.com.apple.Terminal.1A2B"):
            self.assertIsNone(self.service({"XPC_SERVICE_NAME": label}))

    def test_a_child_of_the_launchd_hub_is_not_a_service(self):
        # review of PR #18: a hub-up started from a hub session under launchd inherits its XPC_SERVICE_NAME — `hub stop`
        # advised stopping the working hub
        self.assertIsNone(self.service({"XPC_SERVICE_NAME": "com.hub-core"}, parent=4242, grandparent=777))

    def test_an_orphan_started_from_a_hub_session_is_not_a_service(self):
        # review: `nohup hub-up &` from a hub session under launchd — the shell exits, the orphan goes to launchd (pid 1) with
        # its XPC_SERVICE_NAME; the service is not it. Descendants are given away by the mark hub-up puts on its children
        self.assertIsNone(self.service({"XPC_SERVICE_NAME": "com.hub-core", "HUB_UP_CHILD": "1"}, parent=1))

    def test_systemd_unit_is_known_by_its_own_group(self):
        unit = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/hub-up.service\n"
        env = {"INVOCATION_ID": "x", "SYSTEMD_EXEC_PID": str(os.getpid())}
        self.assertEqual(self.service(env, "linux", unit), "systemctl --user stop hub-up.service")

    def test_a_system_unit_is_stopped_without_user(self):
        env = {"INVOCATION_ID": "x", "SYSTEMD_EXEC_PID": str(os.getpid())}
        self.assertEqual(self.service(env, "linux", "0::/system.slice/hub.service\n"), "sudo systemctl stop hub.service")

    def test_a_child_of_a_unit_is_not_a_service(self):
        # review of PR #18: the group and INVOCATION_ID are inherited by all descendants of the unit — tmux, an editor, hubd under systemd
        unit = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/tmux.service\n"
        env = {"INVOCATION_ID": "x", "SYSTEMD_EXEC_PID": "1"}
        self.assertIsNone(self.service(env, "linux", unit))


class LockWait(unittest.TestCase):
    """HUB_UP_LOCK_WAIT: the tests' second hub-up over a live one gives up on the held lock at once instead of waiting
    the whole LOCK_WAIT; it only shortens the wait, and a restart's new image keeps the time the old one needs."""

    @classmethod
    def setUpClass(cls):
        # hub-up narrows the umask, sets its environment (core/perms) and its import path when it loads: give the
        # process its own back
        umask, path = os.umask(0o022), list(sys.path)
        os.umask(umask)
        try:
            with mock.patch.dict(os.environ):
                loader = importlib.machinery.SourceFileLoader("hub_up_lock", HUB_UP)
                cls.up = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_up_lock", loader))
                loader.exec_module(cls.up)
        finally:
            os.umask(umask)
            sys.path[:] = path

    def test_a_test_shortens_the_wait_and_the_value_is_not_passed_on(self):
        for value, wait in (("0", 0), ("0.5", 0.5)):
            env = {"HUB_UP_LOCK_WAIT": value, "X": "1"}
            self.assertEqual(self.up.lock_wait(env), wait)
            self.assertEqual(env, {"X": "1"})

    def test_anything_else_keeps_the_wait(self):
        for value in ("", "abc", "-1", "nan", "inf", "10", "60", "1e400"):
            self.assertEqual(self.up.lock_wait({"HUB_UP_LOCK_WAIT": value}), 10, value)
        self.assertEqual(self.up.lock_wait({}), 10)

    def test_a_restart_keeps_the_whole_wait(self):
        env = {"HUB_UP_LOCK_WAIT": "0", "HUB_UP_RESTARTED": "1"}
        self.assertEqual(self.up.lock_wait(env), 10)
        self.assertEqual(env, {"HUB_UP_RESTARTED": "1"})


if __name__ == "__main__":
    unittest.main()
