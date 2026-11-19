"""The real claude of this machine for the tests that run it (not the hub's wrapper hosts/nested from a session's PATH);
None — not installed: such tests are skipped (HUB_TEST_CLAUDE sets the path). Apart from the gateway's live test: the
open flavor's run has no gateway (core/cutout.py), the other live tests need it all the same."""
import os

import tests


def real_claude():
    for path in (os.environ.get("HUB_TEST_CLAUDE"), os.path.join(tests.REAL_HOME, ".local", "bin", "claude"),
                 "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if path and os.path.isfile(path) and os.access(path, os.X_OK) and "nested" not in os.path.realpath(path):
            return path
    return None
