"""Settings -> Main -> Hubs on this computer (modules/web/instancesweb.py, core/instancejob.py; the spec of several hubs,
2.5, 2.6, 2.11): the list of the machine's hubs, "Add a hub" and "Remove" as the owner's steps run apart from the
page's server, and an instance's page named by its instance name (the tab, the app's manifest)."""
import tests  # noqa: F401 — keep the machine's settings out, see tests/__init__.py
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import instancecli, instancejob
from modules.web import instancesweb
from modules.web.server import Web
from platforms import current

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ask(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, json.dumps(body) if body is not None else None,
              {"Host": f"127.0.0.1:{port}", "X-Hub": "1", "Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, json.loads(data or b"{}") if path.startswith("/api/") else data


class Page(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="inst-web-")
        self.addCleanup(current.rmtree, self.state)
        self.cfg = tempfile.mkdtemp(prefix="inst-cfg-")
        self.addCleanup(current.rmtree, self.cfg)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": self.cfg})
        env.start()
        self.addCleanup(env.stop)
        instancesweb._seen.clear()
        self.web = Web(self.state)
        self.web.decide_wait = 0
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.web.stop.set)
        self.spawned = []
        self.patch(instancesweb, "spawn", lambda cmd, env: self.spawned.append(cmd))
        self.patch(instancesweb, "refusal", lambda: None)
        self.patch(instancesweb, "engines", lambda found=None: ["claude"])
        other = os.path.join(self.state, "other")
        os.makedirs(os.path.join(other, "cfg"))
        with open(os.path.join(other, "cfg", "hub.json"), "w") as f:
            json.dump({"title": "Personal", "claude_config_dir": "/home/x/c"}, f)
        self.rows = [
            {"name": "", "command": "hub", "title": "", "state": self.state, "config_dir": self.cfg,
             "ports": {"web": 8787}, "running": True, "port_busy": False, "engine": "claude"},
            {"name": "personal", "command": "hub-personal", "title": "personal", "state": other,
             "config_dir": os.path.join(other, "cfg"), "ports": {"web": 8801}, "running": False, "port_busy": False,
             "engine": "claude", "gone": False}]
        self.patch(instancecli, "rows", lambda: self.rows)
        self.dry = []

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def get(self):
        code, got = ask(self.port, "GET", "/api/instances")
        self.assertEqual(code, 200, got)
        return got

    def post(self, body):
        instancesweb._seen.clear()
        return ask(self.port, "POST", "/api/instances", body)

    def test_the_list_marks_this_hub_and_reads_each_hubs_settings(self):
        got = self.get()
        self.assertEqual([h["name"] for h in got["hubs"]], ["", "personal"])
        main, other = got["hubs"]
        self.assertTrue(main["main"] and main["me"])
        self.assertEqual((main["command"], main["port"], main["running"]), ("hub", 8787, True))
        self.assertFalse(other["me"] or other["main"])
        self.assertEqual((other["title"], other["claude"], other["running"]), ("Personal", "own", False))
        self.assertEqual(main["claude"], "shared")
        self.assertIsNone(got["job"])
        self.assertIsNone(got["add"])
        self.assertEqual((got["engines"], got["engine"]), (["claude"], "claude"))
        for h in got["hubs"]:   # nothing of another hub's data: no folders, no sessions
            self.assertNotIn("state", h)
            self.assertNotIn("config_dir", h)

    def test_add_checks_like_the_terminal_then_runs_apart(self):
        self.patch(instancesweb, "check", lambda argv, lang: self.dry.append(argv))
        code, got = self.post({"op": "new", "name": "work", "claude": "own"})
        self.assertEqual(code, 202, got)
        self.assertEqual(self.dry, [["work", "--claude", "own", "--dry-run"]])   # the same refusals, nothing written
        job = instancejob.read(self.state)
        self.assertEqual((job["op"], job["name"], job["claude"], job["engine"]), ("new", "work", "own", "claude"))
        self.assertEqual(len(self.spawned), 1)
        cmd = self.spawned[0]
        self.assertEqual(cmd[-3:], [instancesweb.RUNNER, self.state, job["id"]])
        self.assertEqual(instancejob.argv_of(job), ["new", "work", "--claude", "own", "--yes"])
        self.assertEqual(self.get()["job"]["state"], "running")
        # one job at a time: a second press waits for the first one's end
        code, got = self.post({"op": "new", "name": "work2"})
        self.assertEqual(code, 409, got)
        self.assertEqual(len(self.spawned), 1)

    def test_a_refusal_of_the_terminal_is_said_at_once_and_nothing_starts(self):
        self.patch(instancesweb, "check", lambda argv, lang: "the name work is taken")
        code, got = self.post({"op": "new", "name": "work"})
        self.assertEqual((code, got["error"]), (409, "the name work is taken"))
        self.assertEqual(self.spawned, [])
        self.assertIsNone(instancejob.read(self.state))

    def test_no_main_hub_here_refuses_with_the_reason(self):
        from core import i18n
        self.patch(instancesweb, "refusal", lambda: ("cli.instance.no_main", {}))
        self.assertEqual(self.get()["add"], i18n.tr(self.web.lang_now(), "cli.instance.no_main"))
        code, got = self.post({"op": "new", "name": "work"})
        self.assertEqual((code, got["error"]), (409, i18n.tr(self.web.lang_now(), "cli.instance.no_main")))
        self.assertEqual(self.spawned, [])

    def test_the_check_and_the_job_are_under_one_lock(self):
        # two tabs pressing "Install" during one dry run: the second waits for the lock and then sees the first job
        held = []
        self.patch(instancesweb, "check", lambda argv, lang: held.append(instancesweb._lock.locked()))
        self.assertEqual(self.post({"op": "new", "name": "aa"})[0], 202)
        self.assertEqual(held, [True])
        self.assertEqual(self.post({"op": "new", "name": "bb"})[0], 409)
        self.assertEqual(len(self.spawned), 1)

    def test_a_runner_that_does_not_start_leaves_no_job(self):
        def fail(cmd, env):
            raise OSError("no such program")
        self.patch(instancesweb, "spawn", fail)
        self.patch(instancesweb, "check", lambda argv, lang: None)
        code, got = self.post({"op": "new", "name": "work"})
        self.assertEqual(code, 500, got)
        self.assertIn("no such program", got["error"])
        self.assertIsNone(instancejob.read(self.state))   # nothing "running" that would hold the next press
        self.assertEqual(self.post({"op": "new", "name": "work"})[0], 500)

    def test_codex_only_where_the_form_offers_it(self):
        self.patch(instancesweb, "check", lambda argv, lang: self.dry.append(argv))
        code, _ = self.post({"op": "new", "name": "cx", "engine": "codex"})
        self.assertEqual(code, 400)
        self.patch(instancesweb, "engines", lambda found=None: ["claude", "codex"])
        code, _ = self.post({"op": "new", "name": "cx", "engine": "codex", "claude": "own"})
        self.assertEqual(code, 202)
        self.assertEqual(self.dry, [["cx", "--engine", "codex", "--dry-run"]])   # no Claude sign-in for Codex
        self.assertEqual(instancejob.argv_of(instancejob.read(self.state)), ["new", "cx", "--engine", "codex", "--yes"])

    def test_bad_shapes(self):
        for body in ({"op": "new"}, {"op": "new", "name": ""}, {"op": "new", "name": 5}, {"op": "new", "name": "x" * 41},
                     {"op": "drop"}, {"op": "new", "name": "work", "claude": "theirs"}):
            code, got = self.post(body)
            self.assertEqual(code, 400, (body, got))
        self.assertEqual(self.spawned, [])

    def test_remove_asks_for_the_name_and_only_of_an_instance(self):
        reg = [{"name": "personal", "state": "/home/x/.hub-personal", "gone": False}]
        self.patch(instancesweb.instance, "entries", lambda *a: reg)
        code, got = self.post({"op": "remove", "name": "personal", "confirm": "persona"})
        self.assertEqual(code, 400, got)
        code, got = self.post({"op": "remove", "name": "", "confirm": ""})   # the main hub: `hub uninstall`
        self.assertEqual(code, 400, got)
        code, got = self.post({"op": "remove", "name": "core", "confirm": "core"})
        self.assertEqual(code, 404, got)
        self.assertEqual(self.spawned, [])
        code, got = self.post({"op": "remove", "name": "personal", "confirm": "personal"})
        self.assertEqual(code, 202, got)
        job = instancejob.read(self.state)
        self.assertEqual(instancejob.argv_of(job), ["uninstall", "personal", "--yes"])   # never --purge from the page

    def test_the_end_of_a_job(self):
        instancejob.write(self.state, {"id": "a1", "op": "new", "name": "work", "started": int(time.time()),
                                       "ended": int(time.time()), "code": 1})
        os.makedirs(os.path.join(self.state, "logs"))
        with open(os.path.join(self.state, instancejob.LOG), "w") as f:
            f.write("cloning\n\nthe page did not answer\n")
        job = self.get()["job"]
        self.assertEqual((job["state"], job["lines"]), ("failed", ["cloning", "the page did not answer"]))
        instancejob.write(self.state, {**instancejob.read(self.state), "code": 0})
        self.assertEqual(self.get()["job"]["state"], "done")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        instancejob.write(self.state, {"id": "a2", "op": "remove", "name": "work", "started": int(time.time()),
                                       "pid": dead.pid, "born": None})
        self.assertEqual(self.get()["job"]["state"], "lost")   # its runner is gone without an end: not "running" for good
        me = os.getpid()
        instancejob.write(self.state, {"id": "a2", "op": "remove", "name": "work", "started": int(time.time()) - 3600,
                                       "pid": me, "born": current.process_start(me)})
        self.assertEqual(self.get()["job"]["state"], "running")   # a long install whose runner lives
        with mock.patch.object(current, "process_start", lambda pid: None):
            instancesweb._born.clear()
            self.assertEqual(self.get()["job"]["state"], "running")   # its start time not read this time: still it
        instancesweb._born.clear()
        instancejob.write(self.state, {"id": "a2", "op": "remove", "name": "work", "started": int(time.time()) - 3600,
                                       "pid": me, "born": "another process under the number"})
        self.assertEqual(self.get()["job"]["state"], "lost")
        instancejob.write(self.state, {"id": "a2", "op": "remove", "name": "work", "pid": me, "born": None,
                                       "started": int(time.time()) - instancesweb.JOB_LIMIT - 5})
        self.assertEqual(self.get()["job"]["state"], "lost")   # past the limit, whatever process has the number
        instancejob.write(self.state, {"id": "a2", "op": "remove", "name": "work",
                                       "started": int(time.time()) - instancesweb.START_GRACE - 5})
        self.assertEqual(self.get()["job"]["state"], "lost")   # never started (no pid written)
        code, _ = self.post({"op": "clear"})
        self.assertEqual(code, 200)
        self.assertIsNone(self.get()["job"])
        instancejob.write(self.state, {"id": "a3", "op": "new", "name": "work", "started": int(time.time())})
        self.assertEqual(self.post({"op": "clear"})[0], 409)   # a running one is not forgotten


class Name(unittest.TestCase):
    """An instance's page goes by its instance name: two pages of one machine are not both "Session hub"."""
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="inst-name-")
        self.addCleanup(current.rmtree, self.state)
        self.cfg = tempfile.mkdtemp(prefix="inst-name-cfg-")
        self.addCleanup(current.rmtree, self.cfg)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": self.cfg, "HUB_CONFIG": os.path.join(self.cfg, "hub.json")})
        env.start()
        self.addCleanup(env.stop)

    def anchor(self):
        with open(os.path.join(self.state, "instance.json"), "w") as f:
            json.dump({"name": "personal", "config_dir": self.cfg}, f)

    def test_the_main_hub_as_before(self):
        man = json.loads(Web(self.state).manifest("en"))
        self.assertNotIn("id", man)
        self.assertEqual(man["name"], "Session hub")

    def test_an_instance_is_another_app(self):
        self.anchor()
        web = Web(self.state)
        man = json.loads(web.manifest("en"))
        self.assertNotIn("id", man)   # its own origin (port) already: an id would orphan an app installed earlier
        self.assertEqual(man["name"], "personal")
        with open(os.path.join(self.cfg, "hub.json"), "w") as f:
            json.dump({"title": "Home"}, f)
        man = json.loads(web.manifest("en"))
        self.assertEqual(man["name"], "Home")   # the title the owner gave wins


class Check(unittest.TestCase):
    def test_the_terminals_refusal_in_the_owners_language(self):
        # the real `hub instance new --dry-run` in its own process (the tests' home has no main hub): refused, with
        # the terminal's own words in the request's language, nothing written
        from core import i18n
        why = instancesweb.check(["work", "--dry-run"], "en")
        self.assertEqual(why, i18n.tr("en", "cli.instance.no_main"))
        self.assertEqual(instancesweb.check(["work", "--dry-run"], "ru"), i18n.tr("ru", "cli.instance.no_main"))
        self.assertNotEqual(i18n.tr("ru", "cli.instance.no_main"), why)


class Engines(unittest.TestCase):
    def test_codex_at_every_hub_where_openai_builds_it(self):
        # decided 9.5: the choice at every hub, the program put by the install when the machine has none; only a
        # machine Codex has no build for gets no choice
        from core import codexinstall
        for made, found, want in ((True, False, ["claude", "codex"]), (False, False, ["claude"]),
                                  (False, True, ["claude", "codex"])):   # no build, but a codex on the machine
            with mock.patch.object(instancesweb, "built", lambda made=made: made):
                self.assertEqual(instancesweb.engines(found), want)
        with mock.patch.object(instancesweb, "built", lambda: True), \
                mock.patch.object(codexinstall, "taken", lambda home=None, which=None: None):
            self.assertEqual(instancesweb.engines(), ["claude", "codex"])
            self.assertFalse(instancesweb.codex_found())
        with mock.patch.object(codexinstall, "taken", lambda home=None, which=None: sys.executable):
            self.assertTrue(instancesweb.codex_found())


class Runner(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="inst-job-")
        self.addCleanup(current.rmtree, self.state)

    def test_the_exit_code_goes_into_the_job(self):
        instancejob.write(self.state, {"id": "j1", "op": "new", "name": "work", "claude": "own"})
        seen = []

        def cli(argv, say):
            seen.append(argv)
            say("cloning")
            return 0
        self.assertEqual(instancejob.run(self.state, "j1", cli=cli), 0)
        self.assertEqual(seen, [["new", "work", "--claude", "own", "--yes"]])
        job = instancejob.read(self.state)
        self.assertEqual(job["code"], 0)
        self.assertIn("ended", job)
        with open(os.path.join(self.state, instancejob.LOG)) as f:
            self.assertEqual(f.read(), "cloning\n")
        self.assertEqual(instancejob.run(self.state, "j1", cli=cli), 2)   # an ended job is not run again
        self.assertEqual(len(seen), 1)

    def test_the_runner_writes_its_pid_and_does_not_bring_a_cleared_job_back(self):
        instancejob.write(self.state, {"id": "j4", "op": "new", "name": "work"})
        seen = []

        def cli(argv, say):
            seen.append(instancejob.read(self.state))
            os.unlink(instancejob.path(self.state))   # the page cleared it meanwhile
            return 0
        self.assertEqual(instancejob.run(self.state, "j4", cli=cli), 0)
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertIsNone(instancejob.read(self.state))

    def test_another_jobs_id_or_an_exception(self):
        instancejob.write(self.state, {"id": "j2", "op": "remove", "name": "work"})
        self.assertEqual(instancejob.run(self.state, "other", cli=lambda a, say: 0), 2)
        self.assertNotIn("ended", instancejob.read(self.state))

        def boom(argv, say):
            raise RuntimeError("broken")
        self.assertEqual(instancejob.run(self.state, "j2", cli=boom), 1)
        self.assertEqual(instancejob.read(self.state)["code"], 1)
        with open(os.path.join(self.state, instancejob.LOG)) as f:
            self.assertIn("RuntimeError: broken", f.read())

    def test_the_real_runner_process(self):
        # the file itself, as the page starts it: a hub this machine does not have is refused, and the job says so
        instancejob.write(self.state, {"id": "j3", "op": "remove", "name": "nosuch", "lang": "en"})
        r = subprocess.run([sys.executable, instancesweb.RUNNER, self.state, "j3"], capture_output=True, text=True,
                           timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual((r.stdout, r.stderr), ("", ""))   # everything went into the log
        job = instancejob.read(self.state)
        self.assertEqual(job["code"], 2)
        with open(os.path.join(self.state, instancejob.LOG)) as f:
            self.assertIn("nosuch", f.read())

    def test_under_systemd_a_unit_of_its_own(self):
        env = {"INVOCATION_ID": "x", "PATH": "/usr/bin", "HUB_LANG": "ru", "SECRET": "s"}
        cmd = instancesweb.command("/home/x/.hub-core", "ab12", env, which=lambda n: "/usr/bin/" + n, osname="linux")
        self.assertEqual(cmd[:2], ["systemd-run", "--user"])
        self.assertIn("--setenv=HUB_LANG=ru", cmd)
        self.assertNotIn("--setenv=SECRET=s", cmd)
        self.assertEqual(cmd[-3:], [instancesweb.RUNNER, "/home/x/.hub-core", "ab12"])
        plain = instancesweb.command("/home/x/.hub-core", "ab12", {}, which=lambda n: None, osname="linux")
        self.assertEqual(plain, [sys.executable, instancesweb.RUNNER, "/home/x/.hub-core", "ab12"])
        self.assertEqual(instancesweb.command("/s", "ab12", env, osname="mac")[0], sys.executable)


if __name__ == "__main__":
    unittest.main()
