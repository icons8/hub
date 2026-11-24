"""The panel search matches session names by the rule of the native clients (modules/web/static/js/names.js): the checks
are Node test files in tests/web/*.test.mjs; each one is a test here that runs `node --test <file>`."""
import tests  # noqa: F401 — keep the machine's settings out
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
FILES = sorted((ROOT / "tests" / "web").glob("*.test.mjs"))


@unittest.skipUnless(NODE, "Node is required for the page's own checks")
class WebNames(unittest.TestCase):
    def test_every_check_file_is_there(self):
        self.assertIn("names", {f.name.removesuffix(".test.mjs") for f in FILES}, "the names check would not run")


def _check(path):
    def test(self):
        result = subprocess.run([NODE, "--test", str(path.relative_to(ROOT))], cwd=ROOT, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, (result.stdout[-4000:] + "\n" + result.stderr[-2000:]).strip())
    test.__doc__ = f"node --test tests/web/{path.name}"
    return test


for _path in FILES:   # one test per file: a red one names its file
    setattr(WebNames, "test_" + _path.name.removesuffix(".test.mjs").replace("-", "_"), _check(_path))


if __name__ == "__main__":
    unittest.main()
