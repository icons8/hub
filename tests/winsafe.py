"""What a test does differently where Windows differs — decided by the probes in tests/fakes/can.py, never by the OS
name: a Windows user without Developer Mode (as the update's self-check of a colleague runs) cannot make links, an
administrator or Developer Mode can.

A test makes a link only through `symlink` (the link is what it checks: without the right it skips, with one reason
for every such test) or `try_symlink` (the other checks hold without it). The other Windows pitfalls of tests already
have their own way: replacing a file someone holds open — `platforms.current.replace`; removing a git clone —
`platforms.current.rmtree`; a file git or a POSIX tool reads — `newline="\\n"`; a path into a git config —
`Path.as_posix()`; a file:// address — `Path.as_uri()`."""
import os

from tests.fakes import can

NO_LINKS = "no right to make links here (a Windows user without Developer Mode)"


def symlink(test, target, link, **kw):
    """os.symlink; where this machine cannot make links, `test` is skipped with NO_LINKS and nothing is made."""
    if not can.symlink():
        test.skipTest(NO_LINKS)
    os.symlink(target, link, **kw)


def try_symlink(target, link, **kw):
    """os.symlink, True; False where this machine cannot make links — any other failure stays the test's error."""
    if not can.symlink():
        return False
    os.symlink(target, link, **kw)
    return True
