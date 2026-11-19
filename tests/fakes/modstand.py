"""A core-module stand for Module API tests: hubd (core.daemon.Hub without sessions) + a fake module process
(tests/fakes/fake_module.py), launched the same way hub-up launches it (core/modrun.launch).

    s = Stand(self, points=["doctor"], caps=["doctor"])   # module "fake" in its own folder, enabled in modules.enabled
    s.control(doctor={"do": "silent"})                     # how the process will answer (fake_module.py)
    s.launch(); s.until_ready()                            # hubd saw the launch, hello -> module.ready
    s.hub.modules.mods.call("fake", "doctor", {...})       # a call, the way the core makes it
    s.events("module.error")                               # what was written to the journal
"""
import json
import os
import subprocess
import sys
import tempfile
import types

from core import modrun
from core.daemon import Hub
from tests import waiting
from tests.fakes.procs import kill

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_module.py")


class Stand:
    def __init__(self, case, name="fake", points=(), caps=(), budget=None, requires=None, extra=(), clock=None):
        self.case, self.name = case, name
        self.state_dir = tempfile.mkdtemp(prefix="modstand-")
        self.root = os.path.join(self.state_dir, "mods")
        self.ctl_path = os.path.join(self.state_dir, "ctl.json")
        self.log_path = os.path.join(self.state_dir, "calls.jsonl")
        self.put(name, caps=caps, budget=budget, requires=requires)
        for other in extra:   # more modules: {"name": …, "requires": …}
            self.put(**other)
        self.control(points=list(points))
        self.cfg = {"modules": {"paths": [self.root], "enabled": [name] + [o["name"] for o in extra]}}
        self.hub = Hub(self.state_dir, types.SimpleNamespace(), registry_dir=self.state_dir,
                       **({"clock": clock} if clock else {}))
        self.hub.modules.reload(self.cfg, {})
        self.procs = []
        case.addCleanup(self.cleanup)

    def put(self, name, caps=(), budget=None, requires=None):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        man = {"name": name, "version": "1.0.0", "api": "^2.2", "run": [sys.executable, FAKE, "."],
               "capabilities": list(caps)}
        if budget:
            man["budget"] = budget
        if requires:
            man["requires"] = {"modules": requires}
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump(man, f)

    def control(self, **c):
        with open(self.ctl_path, "w") as f:
            json.dump(c, f)

    def update(self, **c):
        with open(self.ctl_path) as f:
            cur = json.load(f)
        self.control(**{**cur, **c})

    def launch(self, name=None):
        name = name or self.name
        want = self.hub.modules.sync()[name]

        def spawn(cmd, cwd, env, **out):   # out — the dev module's log (stdout, stderr), as in hub-up
            p = subprocess.Popen(cmd, cwd=cwd, env={**env, "FAKE_MODULE_CTL": self.ctl_path,
                                                    "FAKE_MODULE_LOG": self.log_path},
                                 stdin=subprocess.DEVNULL, **{"stdout": subprocess.DEVNULL,
                                                              "stderr": subprocess.DEVNULL, **out})
            self.procs.append(p)
            return p
        return modrun.launch(self.state_dir, name, want, spawn)

    def rec(self, name=None):
        return (self.hub.state.get("modules") or {}).get(name or self.name) or {}

    def until(self, pred, timeout=15.0):
        """The core's module step before each look; the timeout is a fuse (tests/waiting.py)."""
        def look():
            self.hub.modules.tick()
            return pred()
        return bool(waiting.until(look, timeout))

    def until_ready(self, name=None):
        name = name or self.name
        ok = self.until(lambda: self.rec(name).get("ready") and self.rec(name)["ready"] ==
                        (modrun.run_of(self.state_dir, name) or {}).get("launch"))
        self.case.assertTrue(ok, f"module {name} did not become ready: {self.rec(name)}")

    def events(self, kind=None):
        try:
            with open(os.path.join(self.state_dir, "journal.jsonl"), encoding="utf-8") as f:
                evs = [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []
        return [e for e in evs if kind is None or e["type"] == kind]

    def calls(self, point=None):
        try:
            with open(self.log_path, encoding="utf-8") as f:
                got = [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []
        return [c for c in got if point is None or c["point"] == point]

    def spooled(self):
        """Facts in the core's inbox (written by processes other than hubd)."""
        d = os.path.join(self.state_dir, "spool")
        out = []
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if n.endswith(".json"):
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    out.append(json.load(f))
        return out

    def cleanup(self):
        for p in self.procs:
            kill(p.pid)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                pass
