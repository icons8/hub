#!/usr/bin/env python3
"""Replays `tools/test.py --changed` over the last commits of a branch (no tests are run) and says what the selection
would have lost and how much of the suite it keeps.

    tools/dev/selection_replay.py [-n 150] [--ref main] [--list]

For each commit the selection is computed from its files except the test modules (`tests/test_*.py`), on the tree as it was
at that commit. The commit's own test modules — the tests that checked the change — must be in the selection: a miss means
a way of depending that the graph does not see. Commits that need a full run (the runner, tests/fakes, an unknown file
type, too many files) count as the whole suite. The share is by test modules and by test functions."""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from core import testsel  # noqa: E402

TEST = re.compile(r"tests/test_[^/]+\.py")


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout


def replay(root, ref="main", n=150):
    """[{rev, subject, files, selected, total, share, share_tests, full, missed}] for commits that changed something besides
    tests; commits that changed only tests are counted in `tests_only`."""
    rows, tests_only, cache = [], 0, {}
    for rev in git(root, "log", "--first-parent", "--format=%H", f"-{n}", ref).split():
        diff = [d for d in git(root, "diff", "--name-only", "--no-renames", f"{rev}^", rev).split("\n") if d]
        own = [d for d in diff if TEST.fullmatch(d)]
        source = [d for d in diff if d not in own]
        if not source:
            tests_only += 1
            continue
        graph = testsel.Graph(testsel.Commit(root, rev), cache)
        res = testsel.select(root, changed=source, graph=graph)
        chosen = set(res["tests"]) | set(res["sentinels"])
        weight = {t: graph.facts[t][1]["ntests"] for t in graph.tests}
        full = res["full"]
        rows.append({
            "rev": rev[:9], "subject": git(root, "log", "-1", "--format=%s", rev).strip()[:80], "files": source,
            "selected": len(graph.tests) if full else len(chosen), "total": len(graph.tests), "full": full,
            "share": 1.0 if full else len(chosen) / max(1, len(graph.tests)),
            "share_tests": 1.0 if full else sum(weight[t] for t in chosen) / max(1, sum(weight.values())),
            "missed": [] if full else [t for t in own if t in graph.fileset and t not in chosen]})
    return rows, tests_only


def summary(rows, tests_only):
    n = len(rows)
    lines = [f"commits touching more than tests: {n}, only tests: {tests_only}",
             f"full run when in doubt: {sum(1 for r in rows if r['full'])}"]
    if n:
        lines.append(f"average share of the suite: {sum(r['share'] for r in rows) / n:.2f} by modules, "
                     f"{sum(r['share_tests'] for r in rows) / n:.2f} by tests")
    lines.append(f"misses (the commit's own test is not in the selection): {sum(1 for r in rows if r['missed'])}")
    return lines


def main(argv):
    p = argparse.ArgumentParser(description="Replay the tools/test.py --changed selection over the history")
    p.add_argument("-n", type=int, default=150, help="how many recent commits")
    p.add_argument("--ref", default="main")
    p.add_argument("--list", action="store_true", help="line by line per commit")
    a = p.parse_args(argv)
    rows, tests_only = replay(ROOT, a.ref, a.n)
    if a.list:
        for r in rows:
            print(f"{r['rev']} {r['share']:.2f} {r['selected']}/{r['total']} {'FULL ' + r['full'] if r['full'] else ''}"
                  f"{' MISS ' + ','.join(r['missed']) if r['missed'] else ''} {r['subject']}")
    print("\n".join(summary(rows, tests_only)))
    return 1 if any(r["missed"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
