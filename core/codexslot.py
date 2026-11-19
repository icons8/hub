"""Codex (experimental): where the core finds the Codex account pool. Its code is an embed's (core/embed.py, the slot
codex_pool: modules/accounts/codexpool.py — the pool's facade, its checks of the codex.* points, `hub codex add | use |
remove | login <account> | logout <account>`); the open flavor has none of it: a hub on Codex runs on its one sign-in,
the core does not offer the codex.* points (modrun.callable_points), and the page has no "Add a Codex account".

What stays the core's either way: the pool's record in the state (the reducer folds codex.pool, codex.switched…), the
account folders a launch may get (fit — a stray codex-active.json is not trusted), and the cleanup of a pool that has no
code any more (NoPool.tick: a hub that once ran one goes back to its own folder)."""
import os

from core import embed, i18n
from core.coretext import T

SLOT = "codex_pool"


def present():
    """The pool's code is in this tree (asked of the disk every time, core/embed.present)."""
    return embed.present(SLOT)


def code():
    """The pool's module (modules.accounts.codexpool of an embed), or None — the tree has none."""
    if not present():
        return None
    import importlib
    return importlib.import_module(embed.SLOTS[SLOT]["module"])


def pool(state_dir, mods, config, clock, log=None):
    """The process's Codex pool: the embed's Pool, or NoPool where there is none."""
    got = code()
    return NoPool() if got is None else got.Pool(state_dir, mods, config, clock, log=log)


class NoPool:
    """A tree without the pool's code: no rows, no decisions; what the state still has of an earlier pool is dropped
    as the pool itself drops it when its module goes (the sessions go back to the hub folder at their next launch)."""

    def fresh(self):
        pass

    def tick(self, state, record):
        cx = state().get("codex") or {}
        known, acc = cx.get("pool") or {}, cx.get("account") or {}
        if known.get("on") or known.get("profiles"):
            record("codex.pool", {"on": False, "switch_at": known.get("switch_at") or 0.95, "profiles": [], "by": None})
        if acc.get("active"):
            record("codex.switched", {"to": None, "from": acc["active"], "restart": True,
                                     "why": i18n.t("core.codexpool.no_provider")})

    def names(self):
        return None, 0.95, set()

    def silent(self):
        return False

    def admits(self, sender, data, st):
        from core import profiles
        return T("core.codexpool.by_pool", "{kind} from {sender}: Codex is written by the host or the Codex pool "
                 "module").format(kind=(data or {}).get("type") or T("core.codexpool.a_fact", "a fact"),
                                  sender=sender or profiles.A_MODULE)


def fit(state_dir, home):
    """Whether a session may live in the Codex folder home of a pool account, at a launch and for processes without
    the state: the folder is there, a real folder below <state>/moddata with no link below it, the hub user's and closed
    (where mode bits keep that). home may be given through the state folder's own path or through its real one (a link
    above it — a Mac's /var — is the machine's). -> None, or why not."""
    import platforms
    import stat
    own = os.path.abspath(home)
    for base in (os.path.abspath(os.path.join(state_dir, "moddata")), os.path.realpath(os.path.join(state_dir, "moddata"))):
        if own.startswith(base + os.sep):
            break
    else:
        return i18n.t("core.codexpool.not_in_moddata")
    if os.path.realpath(own) != os.path.join(os.path.realpath(base), os.path.relpath(own, base)):
        return i18n.t("core.codexpool.link_in_path")
    try:
        st = os.lstat(own)
    except OSError:
        return i18n.t("core.codexpool.no_folder")
    if not stat.S_ISDIR(st.st_mode):
        return i18n.t("core.codexpool.not_a_folder")
    if platforms.current.file_modes():
        if st.st_uid != os.getuid():
            return i18n.t("core.codexpool.not_hub_user")
        if st.st_mode & 0o077:
            return i18n.t("core.codexpool.open_to_others")
    return None
