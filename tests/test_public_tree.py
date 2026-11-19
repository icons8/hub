"""The tree passes the publish check now, not on release day: the shared patterns (tools/publish-deny.txt) on any
machine, the operator's own patterns (.publish-deny of the main copy, not in the repo) where they exist."""
import tests  # noqa: F401 — ignore the machine's settings
import importlib.util
import os
import subprocess
import unittest

from tools import public_language

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("publish_allow", os.path.join(ROOT, "tools", "publish_allow.py"))
allow_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(allow_tool)


def published():
    """What tools/publish.sh puts in the snapshot and checks (tools/public_language.py names it: the same files count
    for the language of the tree). Only what git knows: the snapshot is `git archive HEAD`, while untracked things in the
    hub folder (.venv, .claude/settings.local.json with the owner's paths) broke the update self-check (18.10)."""
    return public_language.published(ROOT)


def scan(patterns_file, ignore_case):
    """Lines of the published tree that a patterns file names (after the allowed substrings), as publish.sh shows
    them, binary files included."""
    deny = allow_tool.patterns(patterns_file, ignore_case)
    allow = allow_tool.allowed(os.path.join(ROOT, "tools", "publish-allow.txt"))
    entries = []
    for path in published():
        try:
            entries += [(path, n, line) for n, line in enumerate(allow_tool.lines(os.path.join(ROOT, path)), 1)]
        except OSError:
            continue
    return [x.rstrip("\n")[:200] for x in allow_tool.denied(entries, deny, allow)]


def main_checkout():
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT, capture_output=True, text=True,
                            check=True).stdout.strip()
    return os.path.dirname(os.path.abspath(os.path.join(ROOT, common)))


class PublishPatterns(unittest.TestCase):
    def test_the_shared_patterns_find_nothing(self):
        found = scan(os.path.join(ROOT, "tools", "publish-deny.txt"), False)
        self.assertEqual(found, [], "\n".join(found))

    def test_the_operators_patterns_find_nothing(self):
        # patterns of the main copy and of this one, as publish.sh reads them (the test sandbox may lack the former)
        files = {(os.path.join(d, name), name == ".publish-deny") for d in (main_checkout(), ROOT)
                 for name in (".publish-deny", ".publish-deny-case")}
        files = sorted(f for f in files if os.path.isfile(f[0]))
        if not files:
            self.skipTest("no .publish-deny in the main copy or in this one")
        found = [x for path, ignore_case in files for x in scan(path, ignore_case)]
        self.assertEqual(found, [], "\n".join(found))

    def test_the_shared_patterns_catch_what_they_should(self):
        deny = allow_tool.patterns(os.path.join(ROOT, "tools", "publish-deny.txt"), False)
        # assembled from parts so that this file does not match the patterns
        bad = ["/Users/" + "someone/x", "ssh 100." + "101.7.12", "sk-ant-" + "api03-" + "A1b2C3d4" * 4,
               "gh" + "p_" + "a1" * 18, "glpat-" + "x" * 20, "xo" + "xb-1234567-7654321-" + "a" * 24,
               "AK" + "IA" + "ABCDEFGHIJ012345", "write to: some.one@" + "gmail.com",
               "/Users/" + "Someone/x", "/home/" + "someone/x", "C:" + "\\Users\\" + "Someone\\x",
               "C:" + "\\\\Users\\\\" + "someone", "C:" + "/Users/" + "someone", "ssh 10." + "1.2.3",
               "http://192." + "168.1.20:8787", "172." + "20.0.5", "-----BEGIN RSA PRIV" + "ATE KEY-----",
               "-----BEGIN PRIV" + "ATE KEY-----", "bot" + "123456789:" + "AAH" + "x1Y2z3" * 5 + "ab",
               "https://hub-st" + "ats.example.com/v1/report", "http://tele" + "metry.example.org",
               "https://x.ana" + "lytics.io/c"]
        good = ["/Users/" + "x/.local/bin/claude", "http://100.9.9.9:8790", "gh" + "p_<token>", "xo" + "xb-1",
                "hub@example.invalid", "Version 1.100.64", "/home/", "C:" + "\\Users\\<you>", "1.10.0.12",
                "172." + "15.0.1", "http://192.0.2.5:8787", "-----BEGIN PUBLIC KEY-----", "id 123456789:short",
                "https://example.com/stats/today", "tools/comms_stats.py", "alice/hub-stats"]
        self.assertEqual([x for x in bad if not any(p.search(x) for p in deny)], [])
        self.assertEqual([x for x in good if any(p.search(x) for p in deny)], [])


if __name__ == "__main__":
    unittest.main()
