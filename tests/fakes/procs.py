import os


def alive(pid):
    """Whether a process is alive, on any OS. On Windows `os.kill(pid, 0)` is not a check but Ctrl+C
    (GenerateConsoleCtrlEvent): while the parent holds an open handle it does not raise even after the process has died.
    There the exit code STILL_ACTIVE (259) is used; on POSIX, signal 0, and a zombie counts as dead: on Linux via /proc,
    where there is none (macOS) via `ps` (18.10: without /proc every live process looked dead, kill() put out nothing,
    and module processes from tests piled up on the Mac for days)."""
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))) and code.value == 259
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if not os.path.isdir("/proc/self"):
        import subprocess
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[-1][:1] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False   # 12.10: a killed process not yet reaped by the OS kernel gives ESRCH on read; under load that takes seconds
    except OSError:
        return True


def kill(pid, recorded=None):
    """Test cleanup: kill the process if it is still alive (on Windows, with its descendants, and wait for it to be gone).
    recorded is a moment by which the process had surely started (time.time() right after its pid was read from where it
    was written): on Windows a process started later only got the number of the recorded one, which has exited, and is
    left alone, as is one whose start is not readable. The clock of Python 3.12 on Windows is tick-coarse (15.6 ms): a
    process recorded within a tick of its start may be left alone too — a leftover, never a foreign process killed. 2026-11-04: cleanup by pids from a journal ran
    taskkill /T and could take a foreign process with its whole tree. The tree is windows._kill_tree's: by start times,
    never the caller's own chain."""
    if not (type(pid) is int and pid > 1) or not alive(pid):
        return
    if os.name == "nt":
        import ctypes
        from platforms import windows
        k32 = windows._kernel32()
        # while this handle is open the number goes to no one else: the start time read through it is the one the tree is
        # stopped by, and the wait ends when that very process is gone (TerminateProcess returns before that)
        h = k32.OpenProcess(windows._SYNCHRONIZE | windows._PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return
        try:
            born = windows._created(k32, h)   # FILETIME: 100 ns since 1601
            if recorded is not None and (born is None or (born - 116444736000000000) / 1e7 > recorded):
                return
            if windows._kill_tree(pid, born):
                k32.WaitForSingleObject(ctypes.c_void_p(h), 10000)
        finally:
            k32.CloseHandle(h)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
