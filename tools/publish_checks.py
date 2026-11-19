#!/usr/bin/env python3
"""Own checks before a release (tools/publish.sh): the executable `publish-checks` in the hub settings folder receives
the outgoing commit as an argument and decides by its exit code: 0 — go ahead, anything else — no release. No file — nothing to check."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(sha, config_dir=None):
    if config_dir is None:
        sys.path.insert(0, ROOT)
        try:
            from core import catalog
        except ImportError:   # tools/ copied without the core (release test): the settings folder is only the explicit one, as in the core
            if not os.environ.get("HUB_CONFIG_DIR"):
                return 0
            config_dir = os.environ["HUB_CONFIG_DIR"]
        else:
            config_dir = catalog.config_dir()
    path = os.path.join(config_dir, "publish-checks")
    if not os.path.exists(path):
        return 0
    if not os.access(path, os.X_OK):
        print(f"{path} is not executable — the own checks did not run, not publishing (chmod +x or delete the file)", file=sys.stderr)
        return 1
    return subprocess.call([path, sha], cwd=ROOT)   # from the repository root: the check needs the repo itself


if __name__ == "__main__":
    sys.exit(run(sys.argv[1]))
