"""`sudo agents-hub chrome` — and the package's postinst where the system asks for it (installer spec S7).

Ubuntu 23.10+ gives the user namespaces Chrome's sandbox needs only to a program with an AppArmor profile. A profile may
name only a path that users cannot write (anything a user can put there would get past the restriction), so the Chrome
for the agents' browser is put by root into /opt/agents-hub/chrome — the build pinned in the package's own seed (never
code a user can write: this runs as root) — together with a profile for exactly that program. The hub finds it through
PACKAGED "shared" (core/runtime). Running it again does nothing but check; a newer package brings a newer Chrome.

    chrome_root.py      exit 0: Chrome is in place; 1: it is not, the cause is printed"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # /opt/agents-hub
SEED = os.path.join(ROOT, "code-seed")


def trusted(*roots, uid=0, deep=True):
    """Root runs the seed's code: all of it must be root's and closed to others (dpkg lays it out so). A link counts by
    what it leads to; a root that is a file is looked at too; deep=False — only the roots themselves. -> the first path that
    is not, or None."""
    def bad(p):
        try:
            st = os.stat(p)
        except OSError:
            return os.path.islink(p) is False   # a link that leads nowhere runs nothing; a path we cannot look at is no good
        return st.st_uid != uid or bool(st.st_mode & 0o022)

    def leaves(p, root):
        """A link must lead inside the root it was found in (walk does not go into a linked folder: its files would
        never be looked at)."""
        if not os.path.islink(p):
            return False
        real = os.path.realpath(root)
        return os.path.commonpath([real, os.path.realpath(p)]) != real
    for root in roots:
        if bad(root):
            return root
        for top, dirs, files in os.walk(root) if deep else ():
            for name in dirs + files:
                p = os.path.join(top, name)
                if bad(p) or leaves(p, root):
                    return p
    return None


def main():
    sys.dont_write_bytecode = True   # the seed stays as the package laid it out
    # English, whatever the system speaks: the package comes before the hub knows its owner's language (the page then
    # speaks it); the core's steps below take the catalog in this language too, whatever HUB_LANG the caller has
    os.environ["HUB_LANG"] = "en"
    if os.geteuid() != 0:
        print("Needs root: sudo agents-hub chrome", file=sys.stderr)
        return 1
    bad = trusted(ROOT, deep=False) or trusted(SEED, os.path.join(ROOT, "lib"), os.path.join(ROOT, "launch.py"))
    if bad:
        print(f"{bad}: not root's or writable by others — Chrome is not installed", file=sys.stderr)
        return 1
    sys.path.insert(0, SEED)
    from core import fetch, i18n
    from modules.setup import chrome
    pin = chrome.pinned(os.path.join(SEED, "tools", "package", "versions.json"))
    if not pin:
        print(i18n.t("cli.setup.chrome_platform"), file=sys.stderr)
        return 1
    seen = [-1]

    def progress(done, total):
        pct = int(done * 100 / total) if total else 0
        if pct // 10 != seen[0]:
            seen[0] = pct // 10
            print(f"  {pct}%", flush=True)

    try:
        exe = chrome.install_shared(ROOT, pin, say=lambda text: print(text, flush=True), progress=progress)
    except Exception as e:  # noqa: BLE001 — root, often from postinst: the cause in words, never a traceback
        what = str(e) if isinstance(e, (fetch.Failed, chrome.Refused)) else f"{type(e).__name__}: {e}"
        print(f"Chrome is not installed: {what}", file=sys.stderr)
        return 1
    print(f"Chrome {pin['version']}: {exe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
