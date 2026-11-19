"""The example core module examples/module-hello (Module API 2.0) works with the core: hubd sees it via modules.paths,
the process answers hello with the launch secret — the module is ready; a request without the secret is refused."""
import tests  # noqa: F401 — ignore this machine's settings
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest

from core import modrun, modules, version
from core.daemon import Hub
from tests.fakes.procs import kill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "module-hello")


class Example(unittest.TestCase):
    def test_the_manifest_is_valid(self):
        self.assertEqual(modules.read(EXAMPLE, "path")["errors"], [])

    def test_the_example_becomes_ready(self):
        state = tempfile.mkdtemp(prefix="modhello-")
        hub = Hub(state, types.SimpleNamespace(), registry_dir=state)
        hub.modules.reload({"modules": {"paths": [EXAMPLE], "enabled": ["hello"]}}, {})
        want = hub.modules.sync()["hello"]
        procs = []

        def spawn(cmd, cwd, env):   # like hub-up; python3 — by the same interpreter (Windows)
            env = {**env, "HUB_REPO": ROOT, "PYTHONPATH": ROOT}   # hub-up gives the module the core (core.hubmod)
            p = subprocess.Popen([sys.executable] + cmd[1:], cwd=cwd, env=env, stdin=subprocess.DEVNULL)
            procs.append(p)
            self.addCleanup(kill, p.pid)
            return p
        modrun.launch(state, "hello", want, spawn)
        end = time.time() + 15
        while time.time() < end and not (hub.state.get("modules") or {}).get("hello", {}).get("ready"):
            hub.modules.tick()
            time.sleep(0.05)
        rec = hub.state["modules"]["hello"]
        self.assertEqual(rec["ready"], modrun.run_of(state, "hello")["launch"])
        self.assertEqual(rec["version"], "1.0.0")
        run = modrun.run_of(state, "hello")
        self.assertEqual(modrun.post(run["port"], "wrong", "/hook/hello", {}, 2)[0], "status")
        # an owner's word from the handle (hubmod.owner): on its request — into the inbox with the module's channel and key; from the view — 403
        import http.client
        import json
        from core import spool

        def say(headers):
            c = http.client.HTTPConnection("127.0.0.1", run["port"], timeout=5)
            c.request("POST", "/api/mod/hello/say", json.dumps({"to": "maps", "text": "hello", "id": "c1"}),
                      {"X-Hub-Secret": run["secret"], "Content-Type": "application/json", **headers})
            r = c.getresponse()
            r.read()
            c.close()
            return r.status
        self.assertEqual(say({"X-Hub-View": "1"}), 403)
        self.assertEqual(say({"X-Hub-Owner": "1"}), 202)
        (said,) = [e for _, e in spool.take(os.path.join(state, "spool")) if e["type"] == "message.received"]
        self.assertEqual(said["data"], {"to": "maps", "text": "hello", "channel": "mod:hello", "owner": True,
                                        "dedup_key": "c1"})
        self.assertEqual(modules.rows({"modules": {"paths": [EXAMPLE], "enabled": ["hello"]}}, state,
                                      hub.state)[0]["state"], "ready")


if __name__ == "__main__":
    unittest.main()


class Command(unittest.TestCase):
    def hub(self, *args):
        state = self.state
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION")}
        env.update(HUB_STATE_DIR=state, HUB_CONFIG=self.cfg, HUB_CLI_WAIT="0")
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), *args], env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=60)

    def setUp(self):
        import json
        self.state = tempfile.mkdtemp(prefix="modcli-")
        self.cfg = os.path.join(self.state, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({"modules": {"paths": [EXAMPLE, os.path.join(self.state, "nope")], "enabled": ["hello"]}}, f)

    def test_hub_modules_lists_modules_with_their_state_and_reason(self):
        import json
        out = self.hub("modules")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"module API {version.MODULE_API}", out.stdout)
        self.assertIn("hello 1.0.0", out.stdout)
        self.assertIn("no folder", out.stdout)
        table = json.loads(self.hub("modules", "--json").stdout)
        self.assertEqual({r["name"]: r["state"] for r in table}, {"hello": "starting", "nope": "blocked"})

    def test_hub_modules_reset_is_the_owners_fact(self):
        import json
        self.hub("modules", "reset", "hello")
        spool = os.path.join(self.state, "spool")
        with open(os.path.join(spool, sorted(os.listdir(spool))[0]), encoding="utf-8") as f:
            item = json.load(f)
        self.assertEqual((item["type"], item["data"]["module"], item["data"]["owner"]), ("module.reset", "hello", True))


class ModuleProcessIsNotTheOwner(unittest.TestCase):
    """A module process (hub-up gives it HUB_MODULE) calls the hub command: its facts come from the module, not the owner's word."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="modword-")

    def hub(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION")}
        env.update(HUB_STATE_DIR=self.state, HUB_CLI_WAIT="0", HUB_MODULE="hello")
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), *args], env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=60)

    def test_its_facts_are_not_the_owners_word(self):
        import json
        self.hub("notify", "hello")
        spool = os.path.join(self.state, "spool")
        with open(os.path.join(spool, sorted(os.listdir(spool))[0]), encoding="utf-8") as f:
            data = json.load(f)["data"]
        self.assertEqual((data["channel"], data["owner"], data["sender"]), ("plugin", False, "mod:hello"))

    def test_owner_only_commands_refuse(self):
        for args in (["stop"], ["migrate", "--dry"], ["migrate", "--rollback"], ["market", "install", "someone/plugin"]):
            out = self.hub(*args)
            self.assertNotEqual(out.returncode, 0, args)
            self.assertIn("owner", out.stderr, args)

    def test_it_cannot_answer_a_sessions_permission(self):
        from core import reducer
        st, seq = reducer.empty(), [0]

        def put(t, e, d):
            nonlocal st
            seq[0] += 1
            st, _, why = reducer.reduce(st, {"seq": seq[0], "ts": seq[0], "type": t, "entity": e, "data": d})
            return why
        own = {"channel": "cli", "owner": True}
        put("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **own})
        put("session.started", "a", {})
        put("message.received", None, {"to": "a", "text": "x", **own})
        put("turn.started", "a", {})
        put("permission.requested", "a", {"request_id": "r1", "tool": "Bash", "input": {}})
        self.assertIsNotNone(put("permission.answered", "a", {"request_id": "r1", "behavior": "allow",
                                                              "channel": "plugin", "owner": False,
                                                              "sender": "mod:hello"}))
        self.assertEqual(st["sessions"]["a"]["state"], "dialog")
        self.assertIsNone(put("permission.answered", "a", {"request_id": "r1", "behavior": "allow", **own}))
