"""Every test runs once (tests research 03.11): a test class imported into another test module ran there again under
its own name, and a subclass that only adds helpers ran all its base's tests again under its name — TelegramTest's 106
tests ran four times, Market's 20 three times and TaskModel's 6 twice: 364 extra runs (≈5 % of the suite's runs). Such a module
keeps only the tests written in it (load_tests); a hand-written list of classes there must lose none of its own
(test_hubmod's lost StateCopy: written 2026-10-20, never run)."""
import tests  # noqa: F401 — keeps the machine's settings out
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# the only ways another test module's class gets into a module's namespace, and a module choosing its tests itself
BORROWS = re.compile(r"^(\s*(from tests(\.test_\w+)? import|import tests\.test_)|def load_tests\b)", re.M)


def borrowing():
    """Test modules that import from other test modules or choose their tests themselves (load_tests)."""
    out = []
    for f in sorted(os.listdir(HERE)):
        if f.startswith("test") and f.endswith(".py"):
            with open(os.path.join(HERE, f), encoding="utf-8") as src:
                if BORROWS.search(src.read()):
                    out.append(f"tests.{f[:-3]}")
    return out


def flat(suite):
    for t in suite:
        yield from flat(t) if isinstance(t, unittest.TestSuite) else (t,)


def where(test):
    """The class the test's method is written in."""
    return next((c for c in type(test).__mro__ if test._testMethodName in c.__dict__), type(test))


def home(test):
    """The module the test's method is written in."""
    return where(test).__module__


class Once(unittest.TestCase):
    def setUp(self):
        self.names = borrowing()
        self.assertIn("tests.test_telegram_retry", self.names)   # the search finds what it is for
        self.loader = unittest.TestLoader()

    def test_a_module_runs_no_test_written_in_another_one(self):
        elsewhere = {}
        for name in self.names:
            for t in flat(self.loader.loadTestsFromName(name)):
                # written in another test module (it runs there too); a module of mixins that is no test module
                # (tests/embed_contract.py — an embed's tests take them) runs nowhere by itself
                if home(t).startswith("tests.test") and home(t) != name:
                    elsewhere.setdefault(name, set()).add(type(t).__name__)
        self.assertEqual(elsewhere, {}, "foreign tests in the module: load_tests with its own (as in test_telegram_flow)")

    def test_none_of_a_modules_own_tests_is_lost(self):
        # each test written in the module runs under some class of it (a subclass there may leave its base's to the base)
        import importlib
        lost = {}
        for name in self.names:
            mod = importlib.import_module(name)
            written = {f"{c.__qualname__}.{m}" for c in vars(mod).values()
                       if isinstance(c, type) and issubclass(c, unittest.TestCase) and c.__module__ == name
                       for m in self.loader.getTestCaseNames(c) if m in c.__dict__}
            got = {f"{where(t).__qualname__}.{t._testMethodName}" for t in flat(self.loader.loadTestsFromName(name))}
            if written - got:
                lost[name] = sorted(written - got)
        self.assertEqual(lost, {})


if __name__ == "__main__":
    unittest.main()
