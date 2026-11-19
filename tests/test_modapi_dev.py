"""Developer mode for a core module (Module API 2.3): a module from its own folder with `modules.dev` — editing a file
restarts the process by itself (a new want file), the call budget is long, failures neither suspend nor send notices,
the process output goes to its own log, the reason for a crash is shown in `hub modules`; dev is only for modules from
a folder. Commands `hub module add <folder> [--dev]`, `hub module dev <name> [--off]`."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from core import catalog, modrun, modules
from core.daemon import Hub
from tests.fakes.modstand import FAKE, Stand

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")


def scans(sup, t, n=3):
    """n dev scans in a row on a fake clock: each step goes past the scan's own throttle — a slow scan (git check-ignore
    on a loaded Windows) puts the next one further than DEV_SCAN, and a fixed step would skip it."""
    with mock.patch.object(sup, "clock", lambda: t[0]):
        for _ in range(n):
            t[0] = max(t[0] + modrun.DEV_SCAN + 0.1, (sup.next_scan or 0) + 0.1)
            sup.tick()


def dev(stand):
    stand.cfg["modules"]["dev"] = [stand.name]
    stand.hub.modules.reload(stand.cfg, {})


class Fold(unittest.TestCase):
    """Failure folding: a dev module's call failures are recorded but do not accumulate towards a pause; one that did not start is blocked, as with any other."""

    def test_failures_of_a_dev_module_do_not_suspend_it(self):
        rec = None
        for i in range(modrun.FAILS * 3):
            rec = modrun.fold(rec, {"hook": "doctor", "kind": "timeout", "error": "no answer", "dev": True}, 100 + i)
        self.assertEqual((rec["state"], rec["suspensions"], rec["errors"]), ("ok", 0, []))
        self.assertEqual(rec["last"]["error"], "no answer")
        rec = modrun.fold(rec, {"hook": "process", "kind": "exit", "uptime": 1, "error": "code 1", "dev": True}, 200)
        self.assertEqual(rec["state"], "blocked")

    def test_no_notices_for_a_dev_module(self):
        st, actions = {}, []
        for i in range(modrun.FAILS):
            modrun.apply(st, {"type": "module.error", "entity": "m", "ts": 100 + i},
                         {"module": "m", "hook": "doctor", "kind": "timeout", "error": "x", "dev": True}, actions)
        modrun.apply(st, {"type": "module.error", "entity": "m", "ts": 200},
                     {"module": "m", "hook": "process", "kind": "start", "error": "x", "dev": True}, actions)
        self.assertEqual(actions, [])


class Supervisor(unittest.TestCase):
    def test_dev_want_carries_the_flag_and_a_file_edit_changes_its_key(self):
        s = Stand(self)
        dev(s)
        sup = s.hub.modules
        w = sup.sync()["fake"]
        self.assertTrue(w["dev"])
        main = os.path.join(s.root, "fake", "main.py")
        with open(main, "w") as f:
            f.write("x = 1\n")
        t = [1000.0]
        scans(sup, t)   # an edit shows on the second scan in a row with the same fingerprint: the editor has finished writing the file
        self.assertNotEqual(modrun.wants(s.state_dir)["fake"]["key"], w["key"])
        # a changed module.json is re-read: the new version goes into want
        manifest = os.path.join(s.root, "fake", "module.json")
        before = os.stat(manifest).st_mtime_ns
        with open(manifest) as f:
            man = json.load(f)
        with open(manifest, "w") as f:
            json.dump({**man, "version": "1.0.1"}, f)
        # the same size, and the Windows clock ticks in ~16 ms steps: an edit within the same step did not change the
        # fingerprint (like an editor that saved with exactly the same time) — so the edit time is a second later than before
        os.utime(manifest, ns=(before + 10**9, before + 10**9))
        scans(sup, t)
        self.assertEqual(modrun.wants(s.state_dir)["fake"]["version"], "1.0.1")
        self.assertIsInstance(sup.pending(), list)   # the scan's own fields do not mask the wait for modules at startup

    def test_files_the_module_writes_do_not_restart_it(self):
        s = Stand(self)
        dev(s)
        sup = s.hub.modules
        key = sup.sync()["fake"]["key"]
        d = os.path.join(s.root, "fake")
        for rel in ("__pycache__/m.pyc", ".cache/x", "data.db", "run.log", "node_modules/a/b.js"):
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            with open(os.path.join(d, rel), "w") as f:
                f.write("x")
        t = [1000.0]
        scans(sup, t)
        self.assertEqual(modrun.wants(s.state_dir)["fake"]["key"], key)

    def test_files_git_ignores_in_a_working_copy_do_not_restart_it(self):
        s = Stand(self)
        dev(s)
        d = os.path.join(s.root, "fake")
        subprocess.run(["git", "init", "-q", d], check=True, capture_output=True)
        with open(os.path.join(d, ".gitignore"), "w") as f:
            f.write("cache.json\n")
        sup = s.hub.modules
        key = sup.sync()["fake"]["key"]
        t = [1000.0]

        def scans():
            with mock.patch.object(sup, "clock", lambda: t[0]):
                for _ in range(3):
                    t[0] += 60
                    sup.tick()
        with open(os.path.join(d, "cache.json"), "w") as f:
            f.write("{}")
        scans()
        self.assertEqual(modrun.wants(s.state_dir)["fake"]["key"], key)
        with open(os.path.join(d, "main.py"), "w") as f:   # while a code edit means a restart
            f.write("y = 2\n")
        scans()
        self.assertNotEqual(modrun.wants(s.state_dir)["fake"]["key"], key)

    def test_a_folder_inside_an_ignored_folder_of_an_outer_repo_still_restarts(self):
        s = Stand(self)
        dev(s)
        subprocess.run(["git", "init", "-q", s.root], check=True, capture_output=True)
        with open(os.path.join(s.root, ".gitignore"), "w") as f:
            f.write("fake/\n")
        sup = s.hub.modules
        key = sup.sync()["fake"]["key"]
        with open(os.path.join(s.root, "fake", "main.py"), "w") as f:
            f.write("z = 3\n")
        t = [1000.0]
        with mock.patch.object(sup, "clock", lambda: t[0]):
            for _ in range(3):
                t[0] += 60
                sup.tick()
        self.assertNotEqual(modrun.wants(s.state_dir)["fake"]["key"], key)

    def test_a_module_blocked_before_hubd_started_is_watched_too(self):
        s = Stand(self)
        dev(s)
        s.control(hello={"name": "x", "version": "1.0.0"})
        s.launch()
        self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked"))
        s.cleanup()   # hub-up stops a process the core no longer holds and removes its launch
        modrun.halted(s.state_dir, "fake")
        fresh = modrun.Supervisor(s.state_dir, lambda: s.hub.state, s.hub.record)   # the core came up again
        fresh.reload(s.cfg, {})
        t = [time.time() + 10]
        with mock.patch.object(fresh, "clock", lambda: t[0]):
            fresh.tick()
            with open(os.path.join(s.root, "fake", "main.py"), "w") as f:
                f.write("fixed = True\n")
            for _ in range(3):
                t[0] += 60
                fresh.tick()
        self.assertEqual(s.rec()["state"], "ok")

    def test_without_dev_no_watch_and_dev_only_for_a_folder_module(self):
        s = Stand(self)
        self.assertNotIn("dev", s.hub.modules.sync()["fake"])
        found = [{**f, "origin": "git"} for f in s.hub.modules.found]
        cfg = {**s.cfg, "modules": {**s.cfg["modules"], "dev": ["fake"]}}
        sup = modrun.Supervisor(s.state_dir, lambda: s.hub.state, s.hub.record)
        sup.cfg, sup.found, sup.catalog = cfg, found, set()
        self.assertNotIn("dev", sup.want(sup.plan())["fake"])

    def test_a_dev_module_that_did_not_start_tries_again_after_an_edit(self):
        s = Stand(self)
        dev(s)
        s.control(hello={"name": "x", "version": "1.0.0"})   # the wrong hello — blocked
        s.launch()
        self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked"))
        sup = s.hub.modules
        sup.tick()
        self.assertEqual(modrun.wants(s.state_dir), {})
        with open(os.path.join(s.root, "fake", "main.py"), "w") as f:
            f.write("fixed = True\n")
        t = [time.time() + 10]
        scans(sup, t)
        self.assertEqual(s.rec()["state"], "ok")
        self.assertEqual(list(modrun.wants(s.state_dir)), ["fake"])


class Calls(unittest.TestCase):
    @mock.patch.dict(modrun.POINTS, {"doctor": {"budget": 1.0, "cap": "doctor",
                                                "check": lambda res, ctx: res}})
    def test_a_dev_module_gets_the_long_budget_and_is_not_suspended(self):
        s = Stand(self, points=["doctor"], caps=["doctor"])
        dev(s)
        s.launch()
        s.until_ready()
        s.update(doctor={"do": "slow", "delay": 1.5, "answer": {"ok": 1}})
        with mock.patch.object(modrun, "DEV_BUDGET", 5.0):
            self.assertEqual(s.hub.modules.mods.call("fake", "doctor", {}), {"ok": 1})   # longer than the point's budget
        s.update(doctor={"do": "status"})
        for _ in range(modrun.FAILS + 1):
            self.assertIsNone(s.hub.modules.mods.call("fake", "doctor", {}))
        self.assertEqual(s.rec()["state"], "ok")
        self.assertTrue(all(e["data"]["dev"] for e in s.events("module.error")))


class Log(unittest.TestCase):
    def test_output_goes_to_its_log_and_the_exit_reason_names_the_error(self):
        state = tempfile.mkdtemp(prefix="moddev-log-")
        self.addCleanup(shutil.rmtree, state, True)
        d = os.path.join(state, "src")
        os.makedirs(d)
        with open(os.path.join(d, "main.py"), "w") as f:
            f.write("print('start', flush=True)\nraise SystemExit('no TOKEN setting')\n")
        want = {"name": "m", "cmd": [sys.executable, "main.py"], "cwd": d, "version": "1.0.0", "dev": True}
        os.makedirs(modrun.run_dir(state))
        modrun._write(os.path.join(modrun.run_dir(state), "m.want.json"), want)
        started = time.time()
        proc = modrun.launch(state, "m", want, lambda cmd, cwd, env, **kw: subprocess.Popen(cmd, cwd=cwd, env=env,
                                                                                          **kw))
        code = proc.wait(15)
        modrun.exited(state, "m", code, time.time() - started)
        with open(modrun.log_of(state, "m"), encoding="utf-8") as f:
            self.assertIn("start", f.read())
        spooled = os.path.join(state, "spool")
        with open(os.path.join(spooled, sorted(os.listdir(spooled))[0]), encoding="utf-8") as f:
            data = json.load(f)["data"]
        self.assertIn("no TOKEN setting", data["error"])
        self.assertTrue(data["dev"])

    def test_a_log_over_the_cap_is_cut_and_the_process_keeps_writing_to_its_start(self):
        # a module writing an error in a loop bloated the log without limit in one run (~17 MB/day): hub-up cuts it —
        # the old part goes to mod-<name>.log.1, the process keeps writing at the start of the same file, with no hole of zeros
        state = tempfile.mkdtemp(prefix="moddev-log-")
        self.addCleanup(shutil.rmtree, state, True)
        d = os.path.join(state, "src")
        os.makedirs(d)
        with open(os.path.join(d, "main.py"), "w") as f:
            f.write("import os, sys, time\n"
                    "for i in range(400):\n    print('noise ' + 'x' * 500, i, flush=True)\n"
                    "open('p1', 'w').close()\n"
                    "while not os.path.exists('go'):\n    time.sleep(0.01)\n"
                    "print('after the cut', flush=True)\n")
        want = {"name": "m", "cmd": [sys.executable, "main.py"], "cwd": d, "version": "1.0.0", "dev": True}
        os.makedirs(modrun.run_dir(state))
        modrun._write(os.path.join(modrun.run_dir(state), "m.want.json"), want)
        log = modrun.log_of(state, "m")
        with mock.patch.object(modrun, "LOG_MAX", 64 << 10):
            started = time.time()
            proc = modrun.launch(state, "m", want, lambda cmd, cwd, env, **kw: subprocess.Popen(cmd, cwd=cwd, env=env,
                                                                                              **kw))
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            while not os.path.exists(os.path.join(d, "p1")):
                self.assertIsNone(proc.poll())
                time.sleep(0.01)
            self.assertGreater(os.path.getsize(log), 64 << 10)   # 400 lines of ~0.5 KB — over the cap
            modrun.trim_logs()
            self.assertLess(os.path.getsize(log), 1024)
            open(os.path.join(d, "go"), "w").close()
            code = proc.wait(15)
            modrun.trim_logs()
            modrun.exited(state, "m", code, time.time() - started)
        with open(log, "rb") as f:
            now = f.read()
        self.assertNotIn(b"\0", now)
        text = now.decode("utf-8")
        self.assertIn("log cut at", text)
        self.assertIn("after the cut", text)
        self.assertNotIn("noise", text)
        with open(log + ".1", "rb") as f:
            kept = f.read()
        self.assertLessEqual(len(kept), 64 << 10)   # the tail, not the start: .1 holds the last line before the cut
        self.assertTrue(kept.rstrip().endswith(b" 399"), kept[-80:])
        self.assertNotIn("m", modrun._logs)   # it exited — hub-up no longer holds the log file

    def test_a_program_that_does_not_start_gets_no_log_noise_in_the_reason(self):
        state = tempfile.mkdtemp(prefix="moddev-log-")
        self.addCleanup(shutil.rmtree, state, True)
        want = {"name": "m", "cmd": ["no-such-program-x"], "cwd": state, "version": "1.0.0", "dev": True}
        os.makedirs(modrun.run_dir(state))
        modrun._write(os.path.join(modrun.run_dir(state), "m.want.json"), want)
        with self.assertRaises(OSError):
            modrun.launch(state, "m", want, lambda cmd, cwd, env, **kw: subprocess.Popen(cmd, cwd=cwd, env=env, **kw))
        modrun.exited(state, "m", None, 0, error="did not start: no program")
        spooled = os.path.join(state, "spool")
        with open(os.path.join(spooled, sorted(os.listdir(spooled))[0]), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["data"]["error"], "did not start: no program")

    def test_not_dev_keeps_the_old_spawn(self):
        state = tempfile.mkdtemp(prefix="moddev-log-")
        self.addCleanup(shutil.rmtree, state, True)
        got = {}

        def spawn(cmd, cwd, env):   # the old signature: no log
            got["cmd"] = cmd
            return types.SimpleNamespace(pid=1)
        modrun.launch(state, "m", {"cmd": ["x"], "cwd": state}, spawn)
        self.assertEqual(got["cmd"], ["x"])


class Commands(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="moddev-cli-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.cfg = os.path.join(self.state, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({}, f)
        self.mod = os.path.join(self.state, "work", "notes")
        os.makedirs(self.mod)
        with open(os.path.join(self.mod, "module.json"), "w") as f:
            json.dump({"name": "notes", "version": "0.1.0", "api": "^2.3", "run": [sys.executable, FAKE, "."]}, f)

    def hub(self, *args, module=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.state, HUB_CONFIG=self.cfg, HUB_CLI_WAIT="0",
                   **({"HUB_MODULE": module} if module else {}))
        return subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)

    def mods(self):
        return catalog.read_settings(self.cfg).get("modules") or {}

    def test_add_a_folder_with_dev(self):
        r = self.hub("module", "add", self.mod, "--dev")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("notes 0.1.0", r.stdout)
        self.assertEqual(self.mods(), {"paths": [self.mod], "enabled": ["notes"], "dev": ["notes"]})
        again = self.hub("module", "add", self.mod)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.mods()["paths"], [self.mod])   # a repeat does not duplicate
        r = self.hub("module", "dev", "notes", "--off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mods()["dev"], [])
        r = self.hub("module", "dev", "notes")
        self.assertEqual(self.mods()["dev"], ["notes"])
        out = self.hub("modules")
        self.assertIn("dev", out.stdout)
        self.assertIn(modrun.log_of(self.state, "notes"), out.stdout)

    def test_dev_is_only_for_a_folder(self):
        r = self.hub("module", "add", "https://example.invalid/x.git", "--dev")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("folder", r.stderr)
        r = self.hub("module", "dev", "ghost")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.mods(), {})

    def test_a_folder_without_a_module_is_refused(self):
        empty = os.path.join(self.state, "empty")
        os.makedirs(empty)
        r = self.hub("module", "add", empty)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("module.json", r.stderr)
        self.assertEqual(self.mods(), {})

    def test_a_module_process_is_not_the_owner(self):
        for args in (["module", "add", self.mod, "--dev"], ["module", "dev", "notes"]):
            r = self.hub(*args, module="other")
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("owner", r.stderr)
        self.assertEqual(self.mods(), {})


class Rows(unittest.TestCase):
    def test_hub_modules_shows_dev_its_log_and_the_last_failure(self):
        state = tempfile.mkdtemp(prefix="moddev-rows-")
        self.addCleanup(shutil.rmtree, state, True)
        d = os.path.join(state, "m")
        os.makedirs(d)
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump({"name": "m", "version": "1.0.0", "api": "^2.3", "run": ["python3", "main.py"]}, f)
        cfg = {"modules": {"paths": [d], "enabled": ["m"], "dev": ["m"]}}
        st = {"modules": {"m": {"state": "blocked", "reason": "did not start: SyntaxError: invalid syntax",
                                "last": {"hook": "process", "kind": "start", "error": "SyntaxError", "ts": 1}}}}
        row = modules.rows(cfg, state, st)[0]
        self.assertEqual((row["state"], row["dev"], row["log"]), ("blocked", True, modrun.log_of(state, "m")))
        text = modules.text([row])
        self.assertIn("dev", text)
        self.assertIn("SyntaxError", text)


if __name__ == "__main__":
    unittest.main()
