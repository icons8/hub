"""The move of the team's code into its embed (core/embed.py SHIMS, spec d2144426): a hub from git updates its tree in
place under a hub-up, hubd and page started before — until the full restart (it waits for free sessions) they are the
earlier release's code in memory, and they run and import the moved code by its old paths. The test starts that code
of an earlier commit on its own tree, puts the new files in place, and asks it what a live hub asks: the login pool
is there, the gateway stays on, its parts' programs start (2026-11-12: without the shims the gateway went off and the
sessions came up on the machine's own login)."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import platforms
from core import embed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = ("core", "modules", "platforms", "i18n", "hosts", "hooks", "hub-home", "observers", "plugins", "bin", "embed")


def git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def earlier():
    """The commits whose code a hub may run when it meets the move: the last release, and the core just before the
    Embed API came (main of that day) — each only if it still had the code at its old paths."""
    out = {}
    tag = next((t for t in git("tag", "--list", "v[0-9]*", "--sort=-v:refname").split()), None)
    if tag:
        out[tag] = tag
    first = git("log", "--diff-filter=A", "--format=%H", "HEAD", "--", "core/embed.py").split()
    if first:
        out["before the Embed API"] = first[-1] + "^1"
    return {k: v for k, v in out.items() if git("cat-file", "-t", f"{v}:modules/gateway/gateway.py") == "blob"
            and git("cat-file", "-t", f"{v}:bin/hub-up") == "blob"}


# the earlier code in memory: hub-up's parts as it computed them at its start, its core, then — after the new files are
# in place — what a live hub asks of the moved code by its old paths
CHILD = r"""
import json, os, runpy, sys
tree, state = sys.argv[1], sys.argv[2]
sys.path.insert(0, tree)
os.chdir(tree)
sys.argv = ["hub-up", "--help"]
up = runpy.run_path(os.path.join(tree, "bin", "hub-up"), run_name="hub_up_probe")
import modules, modules.voice
from core import profiles
try:
    from core import cutout   # since the open flavor; an earlier release always had the code
except ImportError:
    cutout = None
had_codex_pool = os.path.isfile(os.path.join(tree, "core", "codexpool.py"))   # main between the pool and its move
print("ready", flush=True)
sys.stdin.readline()
out = {"present": {n: cutout.present(n) if cutout else True for n in ("profile", "gateway", "conduit", "speech", "stats")}}
from modules.accounts import accounts
out["pool"] = sorted(accounts.load(os.path.join(state, "none.json")))
from modules.gateway import gateway as gw
out["gateway_settings"] = gw.settings({})
from modules.stats import stats
out["stats"] = stats.settings({}).get("enabled")
from modules.voice import speak
out["speak"] = isinstance(speak.settings({}), dict)
out["gateway_on"] = profiles.Profiles(state).gateway()
out["parts"] = {n: up["PARTS"].get(n) for n in ("gateway", "conduit", "stats")}
if had_codex_pool:   # the Codex pool's old path: a page of that code imports it lazily (POST /api/codex/add)
    import core   # a page that had not imported it yet (hubd has it in memory)
    sys.modules.pop("core.codexpool", None)
    vars(core).pop("codexpool", None)
    from core import codexpool
    out["codex_pool"] = callable(getattr(codexpool, "fit", None)) and isinstance(getattr(codexpool, "Pool", None), type)
print(json.dumps(out), flush=True)
"""


@unittest.skipUnless(embed.SHIMS, "no move in this release: nothing to hand on")
class EarlierCodeOnTheNewTree(unittest.TestCase):
    def test_the_earlier_hub_up_hubd_and_page_keep_the_pool_and_the_gateway(self):
        found = earlier()
        if not found:
            self.skipTest("no earlier commit with the code at its old paths in this clone (a published snapshot)")
        for name, ref in found.items():
            with self.subTest(earlier=name):
                self.check(ref)

    def check(self, ref):
        tmp = tempfile.mkdtemp(prefix="hub-move-")
        self.addCleanup(platforms.current.rmtree, tmp)
        tree, state, cfg = (os.path.join(tmp, x) for x in ("tree", "state", "cfg"))
        for d in (tree, state, cfg):
            os.makedirs(d)
        names = [n for n in KEEP if git("cat-file", "-t", f"{ref}:{n}") == "tree"]
        archive = subprocess.run(["git", "archive", ref, "--", *names], cwd=ROOT, check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", tree], input=archive, check=True)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_UP_PARTS", "HUB_TEST_CUT")}
        env.update({"HUB_STATE_DIR": state, "HUB_CONFIG_DIR": cfg, "PYTHONDONTWRITEBYTECODE": "1"})
        child = subprocess.Popen([sys.executable, "-c", CHILD, tree, state], cwd=tree, env=env, text=True,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        line = child.stdout.readline()
        self.assertEqual(line.strip(), "ready", child.stderr.read() if child.poll() is not None else line)
        for n in KEEP:   # the update: the new files in place, the earlier processes go on
            platforms.current.rmtree(os.path.join(tree, n))
            if os.path.isdir(os.path.join(ROOT, n)):
                shutil.copytree(os.path.join(ROOT, n), os.path.join(tree, n), symlinks=True,
                                ignore=shutil.ignore_patterns("__pycache__"))
        out, err = child.communicate("go\n", timeout=120)
        self.assertEqual(child.returncode, 0, err[-3000:])
        got = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(got["present"], {"profile": True, "gateway": True, "conduit": True, "speech": True,
                                          "stats": True}, got)
        self.assertIn("accounts", got["pool"])
        self.assertIs(got["gateway_settings"]["enabled"], False)
        self.assertIs(got["stats"], False)
        self.assertTrue(got["speak"])
        self.assertIs(got["gateway_on"], True)   # hubd keeps the gateway: the sessions stay on the pool
        self.assertIs(got.get("codex_pool", True), True)

        run = {"env": env, "cwd": tmp, "capture_output": True, "text": True, "timeout": 120, "stdin": subprocess.DEVNULL}
        parts = got["parts"]
        r = subprocess.run([sys.executable, *parts["stats"], "log"], **run)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        r = subprocess.run([sys.executable, *parts["conduit"], "no-such-word"], **run)
        self.assertEqual(r.returncode, 2, r.stderr[-2000:])   # conduit's own usage: its code ran
        # the pool's conduit command an earlier release wrote runs the moved code by its old path
        r = subprocess.run([sys.executable, os.path.join(tree, "modules", "conduit", "conduit.py"), "no-such-word"], **run)
        self.assertEqual(r.returncode, 2, r.stderr[-2000:])
        gateway = subprocess.Popen([sys.executable, *parts["gateway"]], env=env, cwd=tmp, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
        try:
            time.sleep(3)   # portable: a part that cannot import exits at once; alive after 3 s is up
            self.assertIsNone(gateway.poll(), gateway.stderr.read()[-2000:] if gateway.poll() is not None else "")
        finally:
            if gateway.poll() is None:
                gateway.terminate()
                try:
                    gateway.wait(10)
                except subprocess.TimeoutExpired:
                    gateway.kill()
                    gateway.wait()


class Shims(unittest.TestCase):
    def test_each_shim_is_in_the_tree_and_cut_from_the_open_flavor(self):
        from core import appcode
        for rel in embed.SHIMS:
            if embed.roots(ROOT):   # the open flavor has cut them with the embeds: nothing to lead into
                self.assertTrue(os.path.isfile(os.path.join(ROOT, *rel.split("/"))), rel)
            self.assertIn(rel, appcode.OPEN_CUT, rel)

    def test_a_shim_is_never_the_cores_own_code(self):
        for rel in embed.SHIMS:
            got = embed.where(rel)
            self.assertNotEqual(got, os.path.join(ROOT, *rel.split("/")), rel)
