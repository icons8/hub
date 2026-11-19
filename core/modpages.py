"""Modules without a process (Module API 2.10): where the core keeps what it serves for them. Its own file: a page server
of the previous release, already running when the files are replaced, imports modstate lazily from the new tree and must
not look for a new name in the old modrun in its memory."""
import os

from core import modrun, modules

STATIC = ".static.json"   # next to <name>.want.json; a hub-up of an earlier version, which starts a process per want
                          # file, does not see it


def statics(state_dir):
    """{name: static want}: modules without a process that hubd orders kept — only their page plugin is served."""
    try:
        names = sorted(os.listdir(modrun.run_dir(state_dir)))
    except OSError:
        return {}
    out = {}
    for n in names:
        if n.endswith(STATIC):
            w = modrun._read(os.path.join(modrun.run_dir(state_dir), n))
            if w and modules.NAME.fullmatch(n[:-len(STATIC)]) and isinstance(w.get("cwd"), str):
                out[n[:-len(STATIC)]] = w
    return out


def one(state_dir, name):
    """The want of one module kept, with a process or without — for a request per file (the page asks for every asset):
    two small files, not a listing of the folder."""
    if not modules.NAME.fullmatch(name or ""):
        return None
    for suffix in (".want.json", STATIC):
        w = modrun._read(os.path.join(modrun.run_dir(state_dir), name + suffix))
        if w and (isinstance(w.get("cwd"), str) if suffix == STATIC else isinstance(w.get("cmd"), list)):
            return w
    return None


def pages(state_dir):
    """{name: want} of every module kept, with a process or without: where the page plugin and the code folder of a module
    come from (a module without a process is not in modrun.wants() — nothing starts for it)."""
    return {**statics(state_dir), **modrun.wants(state_dir)}
