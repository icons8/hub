"""A test of the core that imports code the open flavor cuts (core/appcode.OPEN_CUT) when its module loads fails on the
open tree, where the cut code is not: it came to light on the opening day's run. Such an import is guarded with
`cutout.present(...)` (tests/test_forms.py), done inside a test under `fakecut.needs(...)` (tests/fakes/cut.py), or the
test lives in the embed that has the code (embed/<name>/tests) or is named in appcode.OPEN_CUT_TESTS."""
import tests  # noqa: F401 — keep this machine's settings out
import ast
import glob
import os
import unittest

from core import appcode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cut_modules():
    """The dotted names of the modules the open flavor cuts: modules/gateway/gateway.py -> modules.gateway.gateway."""
    return {g[:-3].replace("/", ".") for g in appcode.OPEN_CUT if g.endswith(".py")}


def unguarded(tree, cut):
    """[(line, name)] of the imports of cut that run when the module loads: in its body, a class's, or a `with`, `for` or
    `while` block (its `else` too), outside an `if`, `try` or a function (those are guarded or run only by a test)."""
    found = []
    todo = list(tree.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.ClassDef, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):   # run at load
            todo.extend(node.body + getattr(node, "orelse", []))
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        found.extend((node.lineno, n) for n in names if n in cut)
    return sorted(found)


class OpenTests(unittest.TestCase):
    def test_a_test_of_the_core_does_not_import_cut_code_when_it_loads(self):
        cut = cut_modules()
        self.assertTrue(cut)
        allowed = {os.path.normpath(p) for p in appcode.OPEN_CUT_TESTS}
        loose = []
        for path in sorted(glob.glob(os.path.join(ROOT, "tests", "**", "*.py"), recursive=True)):
            rel = os.path.relpath(path, ROOT)
            if os.path.normpath(rel) in allowed:
                continue
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), rel)
            loose.extend(f"{rel}:{n}: {name}" for n, name in unguarded(tree, cut))
        self.assertEqual(loose, [], "guard the import with cutout.present(...), or move the test into the embed that has "
                                    "the code (embed/<name>/tests)")

    def test_the_scan_sees_an_import_and_leaves_a_guarded_one(self):
        cut = {"modules.gateway.gateway"}
        bare = ast.parse("from modules.gateway import gateway as gw\nimport os\n")
        self.assertEqual(unguarded(bare, cut), [(1, "modules.gateway.gateway")])
        direct = ast.parse("import modules.gateway.gateway\nclass A:\n    from modules.gateway import gateway\n")
        self.assertEqual([n for n, _ in unguarded(direct, cut)], [1, 3])
        guarded = ast.parse("gw = None\nif ok:\n    from modules.gateway import gateway as gw\n"
                            "def t():\n    from modules.gateway import gateway\n"
                            "try:\n    import modules.gateway.gateway\nexcept ImportError:\n    pass\n")
        self.assertEqual(unguarded(guarded, cut), [])
        inside = ast.parse("with ctx():\n    from modules.gateway import gateway\n"
                           "for x in y:\n    pass\nelse:\n    import modules.gateway.gateway\n")
        self.assertEqual([n for n, _ in unguarded(inside, cut)], [2, 6])


if __name__ == "__main__":
    unittest.main()
