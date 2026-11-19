"""The Clean theme's own checks (modules/web/static/clean), without a browser: the rules of clean.css read as text (avatars,
the send button, the reaction badge, the image viewer, the model buttons, the composer's bottom row), fit.js on stub nodes,
rows.js (the detailed list), clean.js on stub nodes (voice recording keys, "Finish and sleep", the list avatars). The checks
are Node test files in tests/clean/*.test.mjs; each one is a test here that runs `node --test <file>`."""
import tests  # noqa: F401 — keep the machine's settings out
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
FILES = sorted((ROOT / "tests" / "clean").glob("*.test.mjs"))
EXPECTED = ("alert-contrast", "avatars", "board-hint", "code-blocks", "composer-notice", "composer-rec-overlap", "css-balance", "feed-rows",
            "filter-counts", "finish-sleep", "icons", "image-view", "list-look", "message-actions", "model-button", "nav-menus", "phone-header",
            "question-card", "reaction-corner", "reply-tag", "rows", "screencast", "search-empty", "send-button", "settings-tab-mark",
            "settings-toggles", "status-row", "typing-perf", "usage-bars", "voice-keys")


@unittest.skipUnless(NODE, "Node is required for the Clean theme's checks")
class CleanTheme(unittest.TestCase):
    def run_file(self, path):
        result = subprocess.run([NODE, "--test", str(path.relative_to(ROOT))], cwd=ROOT, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, (result.stdout[-4000:] + "\n" + result.stderr[-2000:]).strip())

    def test_every_check_file_is_there(self):
        names = {f.name.removesuffix(".test.mjs") for f in FILES}
        self.assertEqual(sorted(set(EXPECTED) - names), [], "a check file is missing: its checks would not run")


def _check(path):
    def test(self):
        self.run_file(path)
    test.__doc__ = f"node --test tests/clean/{path.name}"
    return test


for _path in FILES:   # one test per file: a red one names its file, and the runner can split them
    setattr(CleanTheme, "test_" + _path.name.removesuffix(".test.mjs").replace("-", "_"), _check(_path))


if __name__ == "__main__":
    unittest.main()
