"""The `cryptography` library for the parts of the core that need it: the hub bus (HPKE, core/bus.py) and the Secrets
vault (AES-256-GCM, modules/secrets/vault.py). No cryptographic code of our own, only calls of the library.

The packaged hub carries the library in its Python; a hub from git gets it into `<state>/pylib` with the command below
(`hub bus on --install`, the Secrets tab's "Install"). The path is added only in the process that needs it, not for
sessions, and only when the interpreter's own copy is missing or older than the caller needs: an environment that carries
a newer one keeps it (core/busd.heal_lib)."""
import os
import re
import shutil
import sys
import types

import platforms


class Unavailable(Exception):
    """The library is missing, too old or broken: a code and why."""

    def __init__(self, code, why):
        super().__init__(why)
        self.code = code


def pylib(state_dir=None):
    """`<state>/pylib`: where the install command puts the library (not the system Python, not the sessions' paths)."""
    if state_dir is None:
        from core.daemon import default_state_dir
        state_dir = default_state_dir()
    return os.path.join(state_dir, "pylib")


def install_command(state_dir=None, python=None, minimum=(47, 0)):
    """The command that puts cryptography >= minimum into `<state>/pylib` with the hub's interpreter (its wheels must
    match it): pip of this interpreter; without pip but with uv — uv for this interpreter."""
    python = python or sys.executable
    target = pylib(state_dir)
    spec = f"cryptography>={minimum[0]}"
    # --upgrade: an old or half-installed copy already in pylib is replaced, not skipped with a warning and exit 0
    if _has_pip(python) or not shutil.which("uv"):
        return [python, "-m", "pip", "install", "--upgrade", "--target", target, spec]
    return [shutil.which("uv"), "pip", "install", "--upgrade", "--python", python, "--target", target, spec]


def _has_pip(python):
    try:
        return platforms.current.run([python, "-m", "pip", "--version"], 60, capture_output=True).returncode == 0
    except (OSError, ValueError):
        return False


def version(text):
    parts = re.findall(r"\d+", str(text))[:2]
    return tuple(int(p) for p in parts) + (0,) * (2 - len(parts))


def own_new_enough(metadata, minimum):
    try:
        return version(metadata.version("cryptography")) >= minimum
    except metadata.PackageNotFoundError:
        return False


def use_pylib(state_dir, minimum):
    """Put `<state>/pylib` in front of the import path when the interpreter's own library is missing or older than
    `minimum`."""
    from importlib import metadata
    extra = pylib(state_dir)
    if os.path.isdir(extra) and extra not in sys.path and not own_new_enough(metadata, minimum):
        sys.path.insert(0, extra)


AEAD_MIN = (3, 0)   # AESGCM is there since long before; the bus needs 47 for HPKE
BUS_MIN = (47, 0)   # core/bus.MIN_LIB: the vault prefers what the bus needs, or the process would hold an older copy


def aesgcm(state_dir=None):
    """The library's AES-GCM as one object (AESGCM, InvalidTag), or Unavailable with the reason."""
    use_pylib(state_dir, BUS_MIN)   # a copy the bus cannot use, imported first, would stay in this process
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
    except ImportError as e:
        raise Unavailable("no_lib", f"no cryptography: {e}") from None
    return types.SimpleNamespace(AESGCM=AESGCM, InvalidTag=InvalidTag)
