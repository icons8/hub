"""Lint of the tests' portability with a ratchet (research of the test failures, 03.11): the same mistakes kept coming
back in new tests — a symlink without the right to make one, `echo` as a program, `file://` glued from a path, the
Python version written into a fixture — and each was found only on a colleague's Windows or another Python, often as
a rolled-back update. The rules read the code of tests/ (ast), take a second and need no Windows.

Violations that were already there are in tools/portability_baseline.json (by file, rule and a hash of the line's
text, so moving code does not matter and editing such a line makes it new); only new ones are red
(tests/test_portability_lint.py). A deliberate case is marked `# portable: <why>` on its line or the comment line just
above it, or on the `def` line for the whole function.

    python3 tools/portability_lint.py           — the new violations
    python3 tools/portability_lint.py --prune   — drop the fixed ones from the baseline (it never takes new ones)

stdlib only, no imports of the hub."""
import ast
import collections
import hashlib
import json
import os
import re
import sys
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "tools", "portability_baseline.json")

RULES = {
    "T1": "a shell program as the command (echo, sh, rm…): Windows has none without Git Bash — "
          "[sys.executable, \"-c\", …] or tests/fakes/exe.script",
    "T2": "shutil.rmtree: on Windows it leaves read-only files (.git/objects) and long paths — platforms.current.rmtree",
    "T4": "file:// glued from a path: on Windows that is file://C:\\… — pathlib.Path(p).as_uri()",
    "T5": "a Python version written in: hubs run 3.12–3.14 — from sys.version_info",
    "T6": "a fixed pause of 0.3 s or more: slow on a fast machine, red on a slow one — wait for the condition",
    "T7": "a limit on wall-clock seconds: red on a loaded or slow machine — check the order of events, not the time",
    "T8": "a file time read: coarse on Windows and network disks (two writes in one tick have one time) — set the "
          "times the test compares with os.utime, or compare contents; then mark it `# portable: <why>`",
    "T10": "a symlink without the right to make one (a Windows user without Developer Mode: WinError 1314) — "
           "winsafe.symlink(self, …) or winsafe.try_symlink(…) from tests/winsafe.py, "
           "or platforms.current.link_dir for a folder",
}
MARK = re.compile(r"#\s*portable:\s*\S")
SHELL = frozenset("echo sh bash rm cp mv cat ls touch sleep true false kill ps chmod grep sed awk which find head tail "
                  "pgrep pkill stat date env".split())
RUNS = frozenset("run Popen check_output check_call call".split())
# a name that holds a command: a whole word of it (h.claude, cmd_line), not a part (executed, kwargs, progress)
COMMAND = re.compile(r"(?:^|_)(?:cmd|command|argv|args|claude|exe|prog|program|shell|launch|binary)s?(?:_|$)", re.I)
PY_VERSION = re.compile(r"\bpython ?3\.1\d\b", re.I)
BARE_VERSION = re.compile(r"3\.1\d(?:\.\d+)?")
CLOCK = re.compile(r"time\.(?:time|monotonic|perf_counter)\(\)\s*-|\btook\b|\belapsed\b")
NO_LINK = frozenset(("OSError", "PermissionError", "NotImplementedError", "Exception", "BaseException"))
WINDOWS = {"os.name": "nt", "sys.platform": "win32", "WINDOWS": True, "IS_WINDOWS": True, "POSIX": False}

Hit = collections.namedtuple("Hit", "rule path line text")


def _name(node):
    if isinstance(node, ast.Attribute):
        head = _name(node.value)
        return f"{head}.{node.attr}" if head else node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _on_windows(node):
    """The value of a condition on Windows (os.name == "nt", sys.platform.startswith("win"), not WINDOWS…), None — not
    known: a skip or an if is a guard only if it surely keeps the code off Windows."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Name, ast.Attribute)):
        return WINDOWS.get(_name(node))
    if isinstance(node, ast.Call) and not node.keywords:
        if _name(node.func) == "platform.system" and not node.args:
            return "Windows"
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("startswith", "endswith") and len(node.args) == 1:
            s, x = _on_windows(node.func.value), _on_windows(node.args[0])
            return getattr(s, node.func.attr)(x) if isinstance(s, str) and isinstance(x, (str, tuple)) else None
        return None
    if isinstance(node, ast.Tuple):
        items = [_on_windows(e) for e in node.elts]
        return None if None in items else tuple(items)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        x = _on_windows(node.operand)
        return None if x is None else not x
    if isinstance(node, ast.BoolOp):
        xs = [_on_windows(v) for v in node.values]
        if isinstance(node.op, ast.And):
            return False if any(x is not None and not x for x in xs) else None if None in xs else True
        return True if any(x is not None and x for x in xs) else None if None in xs else False
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        a, b = _on_windows(node.left), _on_windows(node.comparators[0])
        if a is None or b is None:
            return None
        op = type(node.ops[0])
        try:
            return {ast.Eq: lambda: a == b, ast.NotEq: lambda: a != b, ast.In: lambda: a in b,
                    ast.NotIn: lambda: a not in b}[op]()
        except (KeyError, TypeError):
            return None
    return None


def _off_windows(decorators):
    """A skip that keeps the test off Windows: skipIf(<true there>), skipUnless(<false there>)."""
    for d in decorators:
        if isinstance(d, ast.Call) and d.args:
            kind = _name(d.func).rsplit(".", 1)[-1]
            there = _on_windows(d.args[0])
            if kind == "skipIf" and there is True or kind == "skipUnless" and there is False:
                return True
    return False


def _imports(tree):
    """{"shutil": names of the module shutil, "can.symlink": names of the probe, "os.symlink": names of os.symlink}"""
    out = {"shutil": {"shutil"}, "can.symlink": {"can.symlink", "tests.fakes.can.symlink"}, "os.symlink": {"os.symlink"}}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out["shutil"] |= {a.asname for a in n.names if a.name == "shutil" and a.asname}
            out["can.symlink"] |= {f"{a.asname}.symlink" for a in n.names if a.name == "tests.fakes.can" and a.asname}
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if n.module == "tests.fakes.can" and a.name == "symlink":
                    out["can.symlink"].add(a.asname or a.name)
                elif n.module == "tests.fakes" and a.name == "can" and a.asname:
                    out["can.symlink"].add(f"{a.asname}.symlink")
                elif n.module == "os" and a.name == "symlink":
                    out["os.symlink"].add(a.asname or a.name)
    return out


def _shell_argv(node):
    return isinstance(node, (ast.List, ast.Tuple)) and node.elts and isinstance(node.elts[0], ast.Constant) \
        and node.elts[0].value in SHELL


def _shell_line(node):
    """A command line given to a shell (shell=True): its first word."""
    if isinstance(node, ast.JoinedStr) and node.values:
        node = node.values[0]
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and (node.value.split() or [""])[0] in SHELL


def _a_command(node, up, runs):
    """Where a list is a command line: what a run gets first, a value given a name that holds a command
    (h.claude = ["echo"], cmd=[…]) or that a run gets first (p = [...]; run(p)) — not any list of words."""
    if isinstance(up, ast.Call):
        return bool(up.args) and up.args[0] is node and _name(up.func).rsplit(".", 1)[-1] in RUNS
    if isinstance(up, ast.keyword):
        return up.value is node and bool(COMMAND.search(up.arg or ""))
    if isinstance(up, (ast.Assign, ast.AnnAssign)) and up.value is node:
        targets = up.targets if isinstance(up, ast.Assign) else [up.target]
        return any(COMMAND.search(_name(t).rsplit(".", 1)[-1]) or _name(t) in runs for t in targets)
    return False


def _against_version_info(node, parents):
    """(3, 12) inside a comparison with sys.version_info: a guard, not a version written in."""
    up = parents.get(node)
    while isinstance(up, (ast.Tuple, ast.List, ast.Set)):
        up = parents.get(up)
    return isinstance(up, ast.Compare) and "version_info" in ast.unparse(up)


def _asks(node, names):
    """The node calls the probe (can.symlink()): a call, not any name that happens to read the same."""
    return any(isinstance(n, ast.Call) and _name(n.func) in names for n in ast.walk(node))


def _catches_no_link(handler):
    names = [] if handler.type is None else \
        [_name(e) for e in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type])]
    return handler.type is None or any(n.rsplit(".", 1)[-1] in NO_LINK for n in names)


def _guarded(node, parents, probe):
    """The code knows where it may not run: under a skip or an if that keeps it off Windows, inside a function that
    asks the probe (a skip, an if) or a class that skips by it, or (probe given) inside a try that catches OSError."""
    child, up = node, parents.get(node)
    while up is not None:
        if probe is not None and isinstance(up, ast.Try) and child in up.body \
                and any(_catches_no_link(h) for h in up.handlers):
            return True
        if isinstance(up, ast.If):
            there = _on_windows(up.test)
            if child in up.body and there is False or child in up.orelse and there is True:
                return True
        if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _off_windows(up.decorator_list):
                return True
            if probe is not None and (isinstance(up, ast.ClassDef) and any(_asks(d, probe) for d in up.decorator_list)
                                      or not isinstance(up, ast.ClassDef) and _asks(up, probe)):
                return True   # a function: its decorators and body; a class: only its decorators, not every method's own
        child, up = up, parents.get(up)
    return False


def _found(tree, parents):
    """(rule, node) of every violation, before the marks."""
    names = _imports(tree)
    runs = {_name(c.args[0]) for c in ast.walk(tree) if isinstance(c, ast.Call) and c.args
            and _name(c.func).rsplit(".", 1)[-1] in RUNS and isinstance(c.args[0], (ast.Name, ast.Attribute))} - {""}
    for n in ast.walk(tree):
        if isinstance(n, (ast.List, ast.Tuple)) and _shell_argv(n) and _a_command(n, parents.get(n), runs) \
                and not _guarded(n, parents, None):
            yield "T1", n
        elif isinstance(n, ast.Call):
            f = _name(n.func)
            if f.rsplit(".", 1)[-1] in RUNS and n.args and _shell_line(n.args[0]) and any(
                    k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in n.keywords) \
                    and not _guarded(n, parents, None):
                yield "T1", n
            if f in ("time.sleep", "sleep") and n.args and isinstance(n.args[0], ast.Constant) \
                    and isinstance(n.args[0].value, (int, float)) and not isinstance(n.args[0].value, bool) \
                    and n.args[0].value >= 0.3:
                yield "T6", n
            if f.rsplit(".", 1)[-1] in ("assertLess", "assertLessEqual") and n.args \
                    and CLOCK.search(ast.unparse(n.args[0])):
                yield "T7", n
            if f == "os.path.getmtime":
                yield "T8", n
            if (f in names["os.symlink"] or isinstance(n.func, ast.Attribute) and n.func.attr == "symlink_to") \
                    and not _guarded(n, parents, names["can.symlink"]):
                yield "T10", n
            if isinstance(n.func, ast.Attribute) and n.func.attr == "format" and isinstance(n.func.value, ast.Constant) \
                    and isinstance(n.func.value.value, str) and "file://" in n.func.value.value:
                yield "T4", n
        elif isinstance(n, ast.Attribute):
            if n.attr == "rmtree" and _name(n.value) in names["shutil"]:
                yield "T2", n
            if n.attr in ("st_mtime", "st_mtime_ns"):
                yield "T8", n
        elif isinstance(n, ast.ImportFrom) and n.module == "shutil" and any(a.name == "rmtree" for a in n.names):
            yield "T2", n
        elif isinstance(n, ast.JoinedStr):
            if any(isinstance(v, ast.FormattedValue) for v in n.values) and any(
                    isinstance(v, ast.Constant) and "file://" in str(v.value) for v in n.values):
                yield "T4", n
        elif isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Mod)):
            if isinstance(n.left, ast.Constant) and isinstance(n.left.value, str) and "file://" in n.left.value:
                yield "T4", n
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            if len(n.value) < 40 and (PY_VERSION.search(n.value) or BARE_VERSION.fullmatch(n.value)):
                yield "T5", n
        elif isinstance(n, ast.Tuple) and len(n.elts) == 2 and all(
                isinstance(e, ast.Constant) and type(e.value) is int for e in n.elts) \
                and n.elts[0].value == 3 and 10 <= n.elts[1].value <= 20 and not _against_version_info(n, parents):
            yield "T5", n


def _marked(lines, first, last):
    """`# portable: <why>` on a line of the node or on a comment line just above it."""
    above = lines[first - 2].strip() if first >= 2 else ""
    return any(MARK.search(x) for x in lines[first - 1:last]) or (above.startswith("#") and bool(MARK.search(above)))


def _functions(node, parents):
    """The functions the node is in, inner first: a mark on a `def` line holds for the whole function."""
    up = parents.get(node)
    while up is not None:
        if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield up
        up = parents.get(up)


def lint(src, path="x.py"):
    """Violations of one file's source: [Hit], a line marked `# portable: <why>` excepted; [] if it does not parse."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # invalid escapes in old tests: SyntaxWarning on every parse
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            return []
    lines = re.split(r"\r\n|\r|\n", src)   # as the parser counts lines: not splitlines(), which also splits at \f, \x1c…
    out = set()
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    for rule, n in _found(tree, parents):
        first, last = n.lineno, getattr(n, "end_lineno", None) or n.lineno
        if _marked(lines, first, last) or any(_marked(lines, f.lineno, f.lineno) for f in _functions(n, parents)):
            continue
        out.add(Hit(rule, path, first, lines[first - 1].strip() if first <= len(lines) else ""))
    return sorted(out, key=lambda h: (h.line, h.rule))


def key(hit):
    return hit.rule, hit.path, hashlib.sha256(hit.text.encode("utf-8")).hexdigest()[:12]


def scan(root=ROOT):
    """Violations in every .py of tests/, paths with "/" from the repo root."""
    out = []
    for d, dirs, files in os.walk(os.path.join(root, "tests")):
        dirs[:] = sorted(x for x in dirs if not x.startswith((".", "__")))
        for f in sorted(files):
            if f.endswith(".py"):
                full = os.path.join(d, f)
                with open(full, encoding="utf-8") as fh:
                    out += lint(fh.read(), os.path.relpath(full, root).replace(os.sep, "/"))
    return out


def load(path=BASELINE):
    """{(rule, path, hash): count}"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return collections.Counter({(rule, p, h): n for rule, files in data.items() for p, lines in files.items()
                                for h, n in lines.items()})


def dump(counts, path=BASELINE):
    data = {}
    for (rule, p, h), n in sorted(counts.items()):
        if n > 0:
            data.setdefault(rule, {}).setdefault(p, {})[h] = n
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")


def new(hits, baseline):
    """The hits the baseline does not hold: as many of one line's text as it counts are old, the rest are new."""
    left, out = collections.Counter(baseline), []
    for h in hits:
        if left[key(h)] > 0:
            left[key(h)] -= 1
        else:
            out.append(h)
    return out


def pruned(hits, baseline):
    """The baseline without what was fixed: each entry down to what is still there, never more than it was."""
    now = collections.Counter(key(h) for h in hits)
    return collections.Counter({k: min(n, now[k]) for k, n in baseline.items() if min(n, now[k]) > 0})


def describe(hit):
    return f"{hit.path}:{hit.line}: {hit.rule} {RULES[hit.rule]}\n    {hit.text}"


def main(argv):
    hits, baseline = scan(), load()
    if argv == ["--prune"]:
        left = pruned(hits, baseline)
        dump(left)
        print(f"baseline: {sum(baseline.values())} before, {sum(left.values())} now")
        return 0
    if argv:
        print(__doc__)
        return 2
    found = new(hits, baseline)
    print("\n".join(describe(h) for h in found) or "no new violations")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
