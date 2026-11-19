"""A guard `codex` first in PATH for the tests and the evals: every codex they start goes with a CODEX_HOME of its own.

Without CODEX_HOME — or with the person's own ~/.codex — Codex writes into the personal folder (even `codex --version`
leaves its helper folder in ~/.codex/tmp; five such starts in one wave of the Codex experiment, 2026-11-05). The guard
refuses such a start with code 3 and the reason on stderr; any other start goes on to the real binary, found as the host
finds it (hosts/codex.codex_bin: PATH, then ~/.local/bin) at the moment the guard is put in place. No real codex on the
machine — no guard: a test that checks "codex is not installed" sees the same as before.

  install(folder, real_home) -> the guard's path or None;  on_path(real_home=None) — a temp folder first in os.environ PATH.
"""
import atexit
import os
import shutil
import stat
import sys
import tempfile

REFUSED = 3
MARK = "hub codex guard"   # in the guard's text: a nested run finds the outer guard and goes through it, not around

BODY = r'''# {mark}
import os, subprocess, sys
REAL, PERSONAL = {real!r}, {personal!r}


def inside(path, folder):
    path, folder = (os.path.normcase(os.path.realpath(p)) for p in (path, folder))
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


home = os.environ.get("CODEX_HOME") or ""
if not home or inside(home, PERSONAL):
    sys.stderr.write("codex (guard for the hub's tests and evals): " + ("CODEX_HOME is not set" if not home else
                     "CODEX_HOME = the personal one " + PERSONAL) + " — codex runs from here only with its own "
                     "CODEX_HOME, otherwise it writes into the personal Codex folder (even with --version)\n")
    sys.exit({refused})
if os.name == "nt":   # no exec there: the real one as a child, its code is ours
    sys.exit(subprocess.call([REAL, *sys.argv[1:]]))
os.execv(REAL, [REAL, *sys.argv[1:]])
'''


def real_codex(real_home, skip=None):
    """The real codex, as hosts/codex.codex_bin finds it: PATH (without the guard's own folder), then
    <real_home>/.local/bin/codex; or None."""
    path = os.pathsep.join(p for p in (os.environ.get("PATH") or "").split(os.pathsep)
                           if p and not (skip and os.path.normcase(os.path.abspath(p)) == os.path.normcase(skip)))
    # which() in the folder, not the bare name: Windows' is codex.exe (or a .cmd), as PATHEXT names it
    return shutil.which("codex", path=path) or shutil.which("codex", path=os.path.join(real_home, ".local", "bin"))


def install(folder, real_home):
    """The guard `codex` in folder (macOS and Linux: a script with a shebang; Windows: codex.py plus codex.cmd); the path
    to launch, or None when the machine has no real codex."""
    real = real_codex(real_home, skip=os.path.abspath(folder))
    if not real:
        return None
    os.makedirs(folder, exist_ok=True)
    body = BODY.format(mark=MARK, real=os.path.abspath(real), personal=os.path.join(real_home, ".codex"),
                       refused=REFUSED)
    path = os.path.join(folder, "codex")
    if os.name != "nt":
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"#!{sys.executable}\n{body}")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path
    with open(path + ".py", "w", encoding="utf-8") as f:
        f.write(body)
    with open(path + ".cmd", "w") as f:
        f.write(f'@"{sys.executable}" "%~dp0codex.py" %*\n')
    return path + ".cmd"


def is_guard(path):
    """The codex at path is a guard (its text, or on Windows its codex.py next to the .cmd, carries MARK)."""
    if os.name == "nt":
        path = os.path.splitext(path)[0] + ".py"
    try:
        with open(path, "rb") as f:
            return MARK.encode() in f.read(4096)
    except OSError:
        return False


def on_path(real_home=None):
    """The guard in a temp folder of its own, first in os.environ PATH (removed at exit); its path or None. Under a guard
    already first in PATH (a run started from the tests, a nested run) — that one: the PATH stays as it was given."""
    first = shutil.which("codex", path=os.environ.get("PATH") or "")
    if first and is_guard(first):
        return first
    folder = tempfile.mkdtemp(prefix="hub-codex-guard-")
    atexit.register(shutil.rmtree, folder, True)
    got = install(folder, real_home or os.path.expanduser("~"))
    if got:
        os.environ["PATH"] = folder + os.pathsep + (os.environ.get("PATH") or "")
    return got
