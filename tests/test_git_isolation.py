"""Tests do not see the machine's git settings (tests/__init__.py). The update self-check runs for everyone with their
own git: on 2026-10-21 v1.3.0 was rolled back on a Mac — a global ignore `**/.claude/settings.local.json` made the file
ignored, and the "not in .gitignore — do not write" test went red. Here is a fully polluted machine: global and system
gitconfig, ignore via XDG, a failing hook, commit signing, a name only from settings."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = textwrap.dedent("""
    import json, os, subprocess, sys
    sys.path.insert(0, sys.argv[1])
    import tests  # noqa: F401 — isolation, as in any run
    repo = sys.argv[2]
    def git(*a):
        r = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True)
        return [r.returncode, r.stdout.strip(), r.stderr.strip()]
    r = subprocess.run(["git", "init", "-q", repo], capture_output=True, text=True)
    out = {"init": [r.returncode, "", r.stderr.strip()]}
    os.makedirs(os.path.join(repo, ".claude"))
    with open(os.path.join(repo, ".claude", "settings.local.json"), "w") as f:
        f.write("{}")
    out["ignored"] = git("check-ignore", "-q", ".claude/settings.local.json")
    out["named"] = git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    out["gpgsign"] = git("config", "commit.gpgsign")
    out["hooks"] = git("config", "core.hooksPath")
    out["xdg"] = os.environ.get("XDG_CONFIG_HOME")
    print(json.dumps(out))
""")


class MachineGitConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        home, xdg = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "xdg")
        os.makedirs(os.path.join(xdg, "git"))
        os.makedirs(home)
        hook = os.path.join(self.tmp, "hooks")
        os.makedirs(hook)
        for name in ("pre-commit", "commit-msg"):
            with open(os.path.join(hook, name), "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(os.path.join(hook, name), 0o755)
        ignore = os.path.join(self.tmp, "ignore")
        for path in (ignore, os.path.join(xdg, "git", "ignore")):
            with open(path, "w") as f:
                f.write("**/.claude/settings.local.json\n")
        dirty = (f"[core]\n\texcludesfile = {ignore}\n\thooksPath = {hook}\n[user]\n\tuseConfigOnly = true\n"
                 "[commit]\n\tgpgsign = true\n[init]\n\tdefaultBranch = master\n").replace("\\", "/")
        for path in (os.path.join(home, ".gitconfig"), os.path.join(xdg, "git", "config"),
                     os.path.join(self.tmp, "system")):
            with open(path, "w") as f:
                f.write(dirty)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(HOME=home, USERPROFILE=home, XDG_CONFIG_HOME=xdg, GIT_CONFIG_SYSTEM=os.path.join(self.tmp, "system"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def probe(self):
        import json
        r = subprocess.run([sys.executable, "-c", PROBE, ROOT, os.path.join(self.tmp, "repo")], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_machine_git_settings_do_not_reach_tests(self):
        out = self.probe()
        self.assertEqual(out["init"][0], 0, out)
        self.assertEqual(out["ignored"][0], 1, "the machine's global ignore is visible to tests")
        self.assertEqual(out["named"][0], 0, out["named"])   # the failing hook and the machine's signing are bypassed
        self.assertEqual(out["gpgsign"][1], "")
        self.assertEqual(out["hooks"][1], "")
        self.assertIsNone(out["xdg"])   # XDG of the machine is where systemd units and the hub settings folder are looked up


class ScratchEnv(unittest.TestCase):
    """git with an environment "from scratch" (`env={...}` without `**os.environ`) loses the isolation from
    tests/__init__.py and reads the machine's system gitconfig: on 2026-10-21 /etc/gitconfig with commit signing and a
    failing hook turned 23 mini-hub tests red. Such a call carries GIT_CONFIG_NOSYSTEM and GIT_CONFIG_GLOBAL itself."""

    def test_git_with_an_env_from_scratch_keeps_the_isolation(self):
        import ast
        import glob
        bare = []
        for path in sorted(glob.glob(os.path.join(ROOT, "tests", "*.py"))):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                env = next((k.value for k in node.keywords if k.arg == "env"), None)
                if not isinstance(env, ast.Dict) or None in env.keys:
                    continue
                if "git" not in {n.value for a in node.args for n in ast.walk(a) if isinstance(n, ast.Constant)}:
                    continue
                keys = {k.value for k in env.keys if isinstance(k, ast.Constant)}
                if not {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"} <= keys:
                    bare.append(f"{os.path.basename(path)}:{node.lineno}")
        self.assertEqual(bare, [])


if __name__ == "__main__":
    unittest.main()
