"""One hubd per state directory: a portable file lock (POSIX and Windows — platforms/)."""
import platforms


def acquire(path):
    """An open file with a lock, or None if already taken. Keep the file open for the whole run."""
    f = open(path, "a+")
    try:
        platforms.current.try_lock(f)
    except OSError:
        f.close()
        return None
    return f


_held = []   # the module keeps the references: the lock lives as long as the process does


def hold(path):
    """Take the lock for the whole life of the process: True — ours, False — already taken by another (05.10:
    `if acquire(...) is None` discarded the file, and the garbage collector released the lock at once)."""
    f = acquire(path)
    if f is None:
        return False
    _held.append(f)
    return True


def exclusive(path):
    """File lock for the duration of a `with` block; if another process holds it — wait (task record: core/taskrec)."""
    return platforms.current.exclusive(path)
