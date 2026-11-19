"""Shared by the eval runs (run.py, prompts.py): does not import the core, so prompt evals run even when core is broken."""
import os
import re
import sys

WORK = os.environ.get("EVALS_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "agents-hub", "evals")
# this is how a logged-out claude answers every turn: "stays silent" checks would pass on it
LOGGED_OUT = re.compile(r"Not logged in|Please run /login|Invalid API key")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# The cases of a Russian-speaking owner are kept apart: evals/locale/ru/{cases,prompts} (each marked "owner_lang": "ru"). The
# folder is not published (tools/publish_allow.SKIP, tools/publish.sh) — the open tree has English cases only; here every
# eval looks in both places.
LOCALE_RU = os.path.join("locale", "ru")


def case_dirs(sub, repo=REPO):
    """The folders that hold the cases of one kind (sub: "cases" or "prompts"): the public one, and the Russian-owner one
    if this copy has it."""
    here = os.path.join(repo, "evals")
    return [d for d in (os.path.join(here, sub), os.path.join(here, LOCALE_RU, sub)) if os.path.isdir(d)]


def case_files(sub, repo=REPO, ext=".json"):
    """Every case file of one kind in both folders, sorted by file name (the names are unique across the folders —
    tests/test_prompt_evals.py — so a bare name means one case)."""
    return sorted((os.path.join(d, n) for d in case_dirs(sub, repo) for n in os.listdir(d) if n.endswith(ext)),
                  key=lambda p: (os.path.basename(p), p))


def find_case(name, repo=REPO):
    """A case by its path, or by its file name (with or without .json) in any case folder: `prompts.py hub-home` finds
    evals/prompts/hub-home.json or evals/locale/ru/prompts/hub-home.json. None if there is none."""
    if os.path.isfile(name):
        return name
    base = name if name.endswith(".json") else name + ".json"
    for sub in ("cases", "prompts"):
        for d in case_dirs(sub, repo):
            if os.path.isfile(os.path.join(d, base)):
                return os.path.join(d, base)
    return None


def machine_memory(home, run, cwd=None):
    """The CLAUDE.md files of the machine, not of the case: claudeMdExcludes for the session under test. Claude Code
    reads <dir>/CLAUDE.md, <dir>/CLAUDE.local.md, <dir>/.claude/CLAUDE.md and <dir>/.claude/rules/*.md of every folder
    above the session's one as project memory: --setting-sources project,local, CLAUDE_CONFIG_DIR or a HOME of its own do
    not stop it (only --setting-sources "", which prompts.py runs with), and for a run under the home folder that is the
    person's own ~/.claude/CLAUDE.md (2026-11-06: an eval's compaction summary quoted it). An eval measures the core's
    texts, so it gets none of them: the home folder and the ones above it, the folders above the run and the ones above
    the session's project. The project — cwd up to its git root, cwd alone outside a repo — keeps its files, but never
    the home folder or a folder above the run (a session whose folder is missing on this machine starts in the home
    folder; a home folder may be a dotfiles repo). Exact paths of what is there at launch, a symlink and where it points:
    claude matches a path as given, while a glob or an escaped pattern silently matches nothing once a folder name has
    ( ) [ ] + or the like in it (2.1.309 probe)."""
    def up(d):
        while True:
            yield d
            if os.path.dirname(d) == d:
                return
            d = os.path.dirname(d)
    machine = list(dict.fromkeys([*up(os.path.abspath(home)), *up(os.path.dirname(os.path.abspath(run)))]))
    keep, chain = [], []
    if cwd:
        for d in up(os.path.abspath(cwd)):
            if d in machine:   # no repo below the machine's folders: the session's folder alone
                break
            chain.append(d)
            if os.path.exists(os.path.join(d, ".git")):
                keep = chain
                break
        keep = keep or [os.path.abspath(cwd)]
    dirs = list(dict.fromkeys(machine + ([*up(os.path.dirname(keep[-1]))] if keep else [])))
    dirs += [r for r in map(os.path.realpath, dirs) if r not in dirs]   # macOS: /var -> /private/var, as claude sees it
    keep = {k for d in keep for k in (d, os.path.realpath(d))} - set(machine)
    found = []
    for d in (d for d in dirs if d not in keep):
        found += [os.path.join(d, name) for name in ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md"))]
        for root, _, files in os.walk(os.path.join(d, ".claude", "rules"), followlinks=True):
            found += [os.path.join(root, f) for f in files]
    found += [r for r in map(os.path.realpath, found) if r not in found]
    return list(dict.fromkeys(p.replace(os.sep, "/") for p in found))   # claude turns "\\" into "/" before matching


def guard_codex():
    """Every codex of the run goes with a CODEX_HOME of its own: the guard first in PATH (tools/codex_guard.py) refuses one
    without it or with the person's ~/.codex. Loaded by its path: the tools folder is not a package of the run."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("codex_guard", os.path.join(REPO, "tools", "codex_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.on_path()


def on_codex():
    """Codex (experimental): this hub runs on Codex (hub.json "host": "codex"). If the core does not load — no,
    prompt evals run as before."""
    sys.path.append(REPO)
    try:
        from core import codexmodel
        return codexmodel.on_codex()
    except Exception:  # noqa: BLE001 — a broken core must not stop prompt evals
        return False

