"""Every PowerShell script file the hub runs goes with -ExecutionPolicy Bypass (one-line spec §5.4): at Windows' default
policy (Restricted) a .ps1 file does not run at all — `irm | iex` is a line, not a file, so the install itself gets
through, and then every file it or the hub starts would be refused (the winstand probe: `-File` without the bypass —
"running scripts is disabled"). The guard reads the shipped files: each `-File` of a powershell command, in code and
in the instructions people copy, has the bypass before it."""
import tests  # noqa: F401
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = ("tests/", "docs/", "CHANGES.md")   # the tests' own fixtures, the local notes, the history of what was said
LOOK_BACK = 400   # characters before -File in which its powershell command starts


def shipped():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode()
    for rel in out.split("\0"):
        if rel and not rel.startswith(SKIP) and rel.endswith((".py", ".ps1", ".cmd", ".bat", ".sh", ".iss", ".md",
                                                               ".js", ".json", ".txt")) or rel.startswith("bin/"):
            path = os.path.join(ROOT, rel)
            if os.path.isfile(path):
                yield rel, path


def runs(text):
    """(offset, the command's text up to its -File) of each powershell command that runs a script file."""
    for m in re.finditer(r"""(?<![\w-])["']?-File["']?(?![\w-])""", text):
        before = text[max(0, m.start() - LOOK_BACK):m.start()]
        start = max(before.lower().rfind("powershell"), before.lower().rfind("pwsh"))
        if start >= 0:
            yield m.start(), before[start:]


def bypassed(command):
    words = re.findall(r"""[\w.-]+""", command)
    lowered = [w.lower() for w in words]
    return any(w == "-executionpolicy" and i + 1 < len(lowered) and lowered[i + 1] == "bypass"
               for i, w in enumerate(lowered))


class Policy(unittest.TestCase):
    def test_every_script_file_runs_with_the_policy_bypassed(self):
        found, bad = 0, []
        for rel, path in shipped():
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for at, command in runs(text):
                found += 1
                if not bypassed(command):
                    bad.append(f"{rel}:{text.count(chr(10), 0, at) + 1}: {command.strip()[-160:]}")
        self.assertEqual(bad, [])
        self.assertGreater(found, 5)   # the guard sees the installers' own runs (it does not look at nothing)

    def test_the_guard_tells_a_missing_bypass(self):
        self.assertEqual([bypassed(c) for _, c in runs('run(["powershell.exe", "-NoProfile", "-File", ps1])')], [False])
        self.assertEqual([bypassed(c) for _, c in runs('& powershell -NoProfile -ExecutionPolicy Bypass -File x.ps1')],
                         [True])
        self.assertEqual([bypassed(c) for _, c in runs('["pwsh", "-ExecutionPolicy", "Bypass", "-File", p]')], [True])
        self.assertEqual(list(runs("Get-Content -File x")), [])   # not a powershell command line


if __name__ == "__main__":
    unittest.main()
