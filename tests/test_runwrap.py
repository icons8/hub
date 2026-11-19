"""`hub run`'s wrapper (platforms/runwrap.py): the life lock is taken before the run, a cancelled run never starts, the
outcome is written only once everything the command started is gone, the deadline holds on a clock set back, a stop
request and a signal stop it, the log keeps at most two parts and stops writing on a full disk. The wrapper is started
as its own process, the way the core starts it: a copy of the file in the run's folder."""
import tests  # noqa: F401 — keep this machine's settings out
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from platforms import runwrap

PY = sys.executable
SOURCE = runwrap.__file__


def folder_with(command, until=60, grace=None, **extra):
    """A run's folder as the core leaves it: the copy of the wrapper and spec.json."""
    d = tempfile.mkdtemp(prefix="hubrun-")
    shutil.copy(SOURCE, os.path.join(d, "wrap.py"))
    spec = {"argv": [PY, "-c", command], "cwd": d, "env": dict(os.environ), "until_at": time.time() + until,
            "umask": -1, **({"grace": grace} if grace is not None else {}), **extra}
    with open(os.path.join(d, runwrap.SPEC), "w") as f:
        json.dump(spec, f)
    return d


def start(d):
    return subprocess.Popen([PY, "-I", os.path.join(d, "wrap.py"), d], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def wait_file(path, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return False


def result(d):
    with open(os.path.join(d, runwrap.RESULT)) as f:
        return json.load(f)


def output(d):
    with open(os.path.join(d, runwrap.LOG), encoding="utf-8", errors="replace") as f:
        return f.read()


def pid_alive(pid):
    if os.name == "nt":
        from platforms import windows
        return windows.alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie (exited, not reaped by its parent yet) counts as gone
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[-1].split()[:1] != ["Z"]
    except OSError:
        return True


class Wrapper(unittest.TestCase):
    def run_to_end(self, d, timeout=40):
        p = start(d)
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            self.fail("the wrapper did not end: " + p.stderr.read().decode(errors="replace"))
        err = p.stderr.read().decode(errors="replace")
        self.assertEqual(p.returncode, 0, err)
        return p

    def test_exit_code_output_and_lock(self):
        d = folder_with("import sys; print('hi'); sys.exit(3)")
        self.run_to_end(d)
        r = result(d)
        self.assertEqual((r["code"], r["reason"]), (3, "exit"))
        self.assertIn("hi", output(d))
        self.assertFalse(os.path.exists(os.path.join(d, runwrap.SPEC)))
        self.assertTrue(os.path.exists(os.path.join(d, runwrap.TAKEN)))
        self.assertFalse(runwrap.alive(d))   # the lock went with the process

    def test_the_lock_is_held_while_it_runs(self):
        d = folder_with("import time; time.sleep(30)")
        p = start(d)
        try:
            self.assertTrue(wait_file(os.path.join(d, runwrap.PIDS)))
            self.assertTrue(runwrap.alive(d))
            self.assertTrue(os.path.exists(os.path.join(d, runwrap.TAKEN)))   # taken only while holding the lock
        finally:
            with open(os.path.join(d, runwrap.STOP), "w") as f:
                json.dump({"by": "test"}, f)
            p.wait(30)

    def test_the_taken_spec_keeps_no_command_and_no_environment(self):
        # the folder stays KEEP_DAYS after the end (core/runjobs.py): a token in the command or a key in the environment
        # would lie on disk all that time; the core reads only how the run was started (how, unit)
        key = "glpat-" + "Zq4" * 8
        d = folder_with(f"import os, time; print(os.environ['MAPS_TOKEN'], '{key}', flush=True); "
                        "time.sleep(30)", how="unit",
                        unit="hub-run-x-r1")
        with open(os.path.join(d, runwrap.SPEC)) as f:
            spec = json.load(f)
        spec["env"]["MAPS_TOKEN"] = key
        with open(os.path.join(d, runwrap.SPEC), "w") as f:
            json.dump(spec, f)
        p = start(d)
        try:
            self.assertTrue(wait_file(os.path.join(d, runwrap.PIDS)))
            self.assertTrue(wait_file(os.path.join(d, runwrap.LOG)))
            end = time.time() + 20
            while key not in output(d) and time.time() < end:
                time.sleep(0.05)
            self.assertEqual(output(d).count(key), 2)   # the command got both, as written
            with open(os.path.join(d, runwrap.TAKEN)) as f:
                taken = f.read()
            self.assertNotIn(key, taken)
            self.assertEqual(json.loads(taken), {k: v for k, v in spec.items() if k not in ("argv", "env")})
        finally:
            with open(os.path.join(d, runwrap.STOP), "w") as f:
                json.dump({"by": "test"}, f)
            p.wait(30)
        self.assertEqual(result(d)["reason"], "stopped")

    def test_a_cancelled_run_never_starts(self):
        d = folder_with("open('ran', 'w').write('x')")
        os.rename(os.path.join(d, runwrap.SPEC), os.path.join(d, runwrap.CANCELLED))   # the core's cancel won
        self.run_to_end(d)
        self.assertFalse(os.path.exists(os.path.join(d, "ran")))
        self.assertFalse(os.path.exists(os.path.join(d, runwrap.RESULT)))

    def test_a_held_lock_is_not_taken_by_a_second_wrapper(self):
        d = folder_with("open('ran', 'w').write('x')")
        held = runwrap.hold(d)
        try:
            p = start(d)
            p.wait(20)
            self.assertEqual(p.returncode, 3)
            self.assertTrue(os.path.exists(os.path.join(d, runwrap.SPEC)))   # not taken
            self.assertFalse(os.path.exists(os.path.join(d, "ran")))
        finally:
            held.close()

    def test_the_deadline_stops_it(self):
        d = folder_with("import time; time.sleep(60)", until=1)
        t0 = time.time()
        self.run_to_end(d)
        r = result(d)
        self.assertEqual(r["reason"], "timeout")
        self.assertLess(time.time() - t0, 15)   # portable: the deadline is what is tested; 15 times the 1 s term

    def test_a_stop_request_stops_it_and_names_who(self):
        d = folder_with("import time; time.sleep(60)")
        p = start(d)
        self.assertTrue(wait_file(os.path.join(d, runwrap.PIDS)))
        with open(os.path.join(d, runwrap.STOP), "w") as f:
            json.dump({"by": "maps"}, f)
        p.wait(30)
        r = result(d)
        self.assertEqual((r["reason"], r.get("by")), ("stopped", "maps"))

    def test_the_outcome_comes_after_what_the_command_left_is_gone(self):
        # the command exits at once, leaving a child that writes a heartbeat: the wrapper stops the child first
        beat = "import os, sys, time\nwhile True:\n    open(sys.argv[1], 'a').write(str(time.time()) + '\\n'); time.sleep(0.2)\n"
        d = folder_with("import subprocess, sys, os, time\n"
                        f"c = subprocess.Popen([sys.executable, '-c', {beat!r}, os.path.join(os.getcwd(), 'beat')])\n"
                        "open('child', 'w').write(str(c.pid)); time.sleep(1)")
        self.run_to_end(d)
        r = result(d)
        with open(os.path.join(d, "child")) as f:
            child = int(f.read())
        self.assertFalse(pid_alive(child))
        with open(os.path.join(d, "beat")) as f:
            last = float(f.read().split()[-1])
        self.assertLessEqual(last, r["ended_at"])

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "a process cannot ignore TerminateProcess")
    def test_a_command_deaf_to_sigterm_is_killed_after_the_grace(self):
        d = folder_with("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
                        until=1, grace=1)
        t0 = time.time()
        self.run_to_end(d)
        self.assertEqual(result(d)["reason"], "timeout")
        self.assertLess(time.time() - t0, 12)   # portable: SIGKILL after the 1 s grace, not after the 60 s sleep

    @unittest.skipUnless(hasattr(signal, "SIGTERM") and os.name != "nt", "a signal to the wrapper is POSIX")
    def test_sigterm_to_the_wrapper_is_a_stop(self):
        d = folder_with("import time; time.sleep(60)")
        p = start(d)
        self.assertTrue(wait_file(os.path.join(d, runwrap.PIDS)))
        p.send_signal(signal.SIGTERM)
        p.wait(30)
        r = result(d)
        self.assertEqual((r["reason"], r.get("by")), ("stopped", "signal"))

    def test_a_removed_copy_of_the_code_does_not_matter_after_the_start(self):
        d = folder_with("import time; time.sleep(2); print('done')")
        p = start(d)
        self.assertTrue(wait_file(os.path.join(d, runwrap.PIDS)))
        os.unlink(os.path.join(d, "wrap.py"))
        p.wait(30)
        self.assertEqual(result(d)["code"], 0)
        self.assertIn("done", output(d))

    def test_a_command_that_cannot_start_is_reported(self):
        d = folder_with("pass")
        with open(os.path.join(d, runwrap.SPEC)) as f:
            spec = json.load(f)
        spec["argv"] = [os.path.join(d, "no-such-program")]
        with open(os.path.join(d, runwrap.SPEC), "w") as f:
            json.dump(spec, f)
        self.run_to_end(d)
        self.assertEqual(result(d)["reason"], "not_started")


class Deadline(unittest.TestCase):
    def test_a_wall_clock_set_back_does_not_stretch_the_run(self):
        # started at 1000 for 90 minutes; the wall clock was set back three hours: the awake clock ends it at 90 min
        self.assertFalse(runwrap.due(1000 - 3 * 3600 + 5000, 5000, 1000 + 5400, 0, 5400))
        self.assertTrue(runwrap.due(1000 - 3 * 3600 + 5400, 5400, 1000 + 5400, 0, 5400))
        self.assertTrue(runwrap.due(1000 + 5400, 10, 1000 + 5400, 0, 5400))   # the wall deadline


class LogLimits(unittest.TestCase):
    def test_two_parts_at_most(self):
        d = tempfile.mkdtemp(prefix="hubrun-log-")
        log = runwrap.Log(d, part=100, free_min=0)
        for i in range(50):
            log.write(f"line {i:03}\n".encode())
        log.close()
        new, old = output(d), open(os.path.join(d, runwrap.OLD_LOG)).read()
        self.assertIn("line 049", new)
        self.assertIn("earlier output dropped", new)
        self.assertLessEqual(len(new.encode()), 100 + len(runwrap.DROPPED) + 10)
        self.assertLessEqual(len(old.encode()), 100 + len(runwrap.DROPPED) + 10)
        self.assertNotIn("line 000", new + old)

    def test_a_full_disk_stops_the_output_with_one_line(self):
        d = tempfile.mkdtemp(prefix="hubrun-log-")
        log = runwrap.Log(d, free_min=1 << 62)   # more than any disk has free
        log.write(b"secret output\n")
        log.write(b"more\n")
        log.close()
        text = output(d)
        self.assertNotIn("secret output", text)
        self.assertEqual(text.count("no longer written"), 1)


class Result(unittest.TestCase):
    def test_a_full_disk_is_tried_again(self):
        d = tempfile.mkdtemp(prefix="hubrun-res-")
        run, calls, real = runwrap.Run(d, sleep=lambda s: None), [], runwrap.write_json

        def flaky(path, data):
            calls.append(path)
            if len(calls) < 3:
                raise OSError(28, "No space left on device")
            real(path, data)
        runwrap.write_json = flaky
        try:
            self.assertTrue(run.result({"code": 0, "reason": "exit"}))
        finally:
            runwrap.write_json = real
        self.assertEqual(len(calls), 3)
        self.assertEqual(result(d)["code"], 0)


if __name__ == "__main__":
    unittest.main()
