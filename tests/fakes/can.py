"""What this machine lets a test do, found by trying it, not by the OS name: a Windows user without the right to make
links (a normal account, no Developer Mode) and an administrator differ, and so do Windows and POSIX file locking."""
import functools
import os
import shutil
import subprocess
import sys
import tempfile


@functools.cache
def symlink():
    """os.symlink works here (Windows: only an administrator or Developer Mode — otherwise WinError 1314)."""
    d = tempfile.mkdtemp(prefix="can-")
    try:
        os.symlink("x", os.path.join(d, "link"))
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(d, True)


@functools.cache
def replace_open():
    """A file another handle holds open can be replaced (POSIX; Windows refuses while it is open — Python's open() has
    no FILE_SHARE_DELETE — and the core waits for the reader or leaves the file, platforms.windows.replace)."""
    d = tempfile.mkdtemp(prefix="can-")
    new, held = os.path.join(d, "new"), os.path.join(d, "held")
    for p in (new, held):
        with open(p, "w") as f:
            f.write(p)
    try:
        with open(held):
            os.replace(new, held)
        return True
    except OSError:   # PermissionError on Windows; EBUSY on some network mounts
        return False
    finally:
        shutil.rmtree(d, True)


@functools.cache
def inherit_umask():
    """A child process starts with this process's umask (POSIX: fork keeps it; Windows: a new process starts with its own)."""
    old = os.umask(0o027)
    try:
        r = subprocess.run([sys.executable, "-c", "import os; print(os.umask(0))"], capture_output=True, text=True,
                           timeout=60)
    finally:
        os.umask(old)
    return r.stdout.strip() == str(0o027)


@functools.cache
def shebang():
    """A file with a `#!` line runs as a program (POSIX; Windows does not read the line)."""
    d = tempfile.mkdtemp(prefix="can-")
    path = os.path.join(d, "x")
    with open(path, "w", newline="\n") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)
    try:
        return subprocess.run([path], stdin=subprocess.DEVNULL, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(d, True)   # portable: one file of our own, nothing read-only


@functools.cache
def hook_shell():
    """The command line before a hook's line, the way Codex runs it here: /bin/sh -c (POSIX), Windows PowerShell
    -Command (Windows: Codex runs a hook's line in PowerShell, hosts/codex_rules._hook_line), or None — no such shell."""
    for argv in (["/bin/sh", "-c"], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]):
        try:
            if subprocess.run(argv + ["exit 0"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=60).returncode == 0:
                return argv
        except (OSError, subprocess.SubprocessError):
            continue
    return None


@functools.cache
def run_without_path(program="git"):
    """A process without PATH still finds program (POSIX: the default path, /bin:/usr/bin; Windows has none)."""
    env = {k: v for k, v in os.environ.items() if k.upper() != "PATH"}
    code = f"import subprocess; subprocess.run([{program!r}, '--version'], capture_output=True, check=True)"
    try:
        return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
