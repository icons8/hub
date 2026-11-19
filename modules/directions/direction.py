"""The "Directions" module (a separate module with a skill).
A direction is one big task of one long-lived session; its working memory is a backlog (slice tasks with AC,
decisions, goal and invariant documents; files in the Backlog.md format) in the hub folder, not in the project repo.
After context compaction or in a new session the logic is restored from here (`resume`), not from the compaction summary. The backlog is
not a board for the owner: questions and results go to the owner as always (turn result, questions with options).

    hub direction new <slug>                 create a backlog and the direction's own git folder (workspaces/<slug>)
    hub direction new <slug> --repo <path>   create a backlog (no git); the direction works in this repo (with a commit)
    hub direction new <slug> --repo <path> --adopt   take the repo's ready backlog (<repo>/backlog), without a second one
    hub direction new <slug> --repo <path> --session <name>   the hub creates a direction for a session with a different name
    hub direction lead <slug> [<session>]    hand the direction to the named session (the hub and the owner); no name — who leads
    hub direction where <slug>               the direction's repo (the direction's session is opened there)
    hub direction repo <slug> <path>         move the direction to another repo
    hub direction rename <slug> <new>        rename: the backlog and the board; the session with hub session rename
    hub direction decide <slug> "<title>" --decision "…" [--context "…"] [--consequences "…"]   a decision with text
    hub direction resume <slug>              documents + open tasks + latest decisions in one output
    hub direction list                       directions and their tasks by status
    hub direction archive <slug>             into directions/_archive/
    hub direction board <slug> [--stop]      a link to the direction's board on the hub page; boards are boards of the old kind
    hub direction <slug> <backlog command>   task create|edit|list|view|archive, doc create|update|list|view, decision
                                             list|view inside the direction; `--help` gives the full list

Where they live: `directions.root` in hub.json (~/.hub-core/directions by default), the HUB_DIRECTIONS variable wins.
The hub keeps the backlog itself (minibacklog.py); the external backlog utility is not needed and is not called (16.10).
"""
import glob, json, os, re, shutil, subprocess, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from core import i18n  # noqa: E402
from modules.directions import minibacklog  # noqa: E402

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,39}")
RESERVED = ("archive", "board", "boards", "decide", "lead", "list", "new", "rename", "repo", "resume", "where")
USAGE = "directions.cli.usage"   # a catalog key: translated where it is shown (main)


class Fail(Exception):
    pass


def root(cfg=None, state=None):
    """The boards folder. cfg is the settings the caller holds (the page: an unreadable hub.json is no reason for it to fail); without
    it, from disk, and an unreadable file is a refusal: the CLI must not write boards anywhere but where the owner chose. state is the hub
    state folder (a daemon knows its own), without it the default."""
    if os.environ.get("HUB_DIRECTIONS"):
        return os.path.abspath(os.path.expanduser(os.environ["HUB_DIRECTIONS"]))
    from core import catalog
    from core.daemon import default_state_dir
    state = state or default_state_dir()
    path = ((catalog.load_config(state) if cfg is None else cfg).get("directions") or {}).get("root") or os.path.join(state, "directions")
    return os.path.abspath(os.path.expanduser(path))


def check_slug(slug):
    if not SLUG.fullmatch(slug) or slug in RESERVED:
        raise Fail(i18n.t("directions.cli.bad_slug", slug=slug, reserved=", ".join(RESERVED)))
    return slug


def active():
    try:
        names = os.listdir(root())
    except FileNotFoundError:
        return []
    return sorted(n for n in names if not n.startswith("_") and os.path.isdir(os.path.join(root(), n, "backlog")))


def home(slug):
    check_slug(slug)
    path = path_of(slug)   # the former name of a renamed direction is an address too
    if not os.path.isdir(os.path.join(path, "backlog")):
        raise Fail(i18n.t("directions.cli.not_found", slug=slug,
                          names=", ".join(active()) or i18n.t("directions.cli.none")))
    return path


def names_of(slug):
    """Every name a direction's session may hold as its backlog: the one given, the folder's and its former ones (a
    session keeps the name under which it took the direction, see path_of)."""
    path = home(slug)
    names = [slug, os.path.basename(path), *(load_meta(path).get("was") or [])]
    return [n for i, n in enumerate(names) if n not in names[:i]]


def path_of(slug, base=None):
    """A direction's folder by name, and by its former name too (hub direction rename writes it to was): a direction session's
    backlog keeps the name under which it created it. No name check; if there is no such one, the folder under this name."""
    base = base or root()
    path = os.path.join(base, slug)
    if os.path.isdir(os.path.join(path, "backlog")):
        return path
    try:
        names = sorted(n for n in os.listdir(base) if not n.startswith("_")
                       and os.path.isdir(os.path.join(base, n, "backlog")))
    except OSError:
        names = []
    for name in names:
        if slug in (load_meta(os.path.join(base, name)).get("was") or []):
            return os.path.join(base, name)
    return path


def backlog(path, args, capture=False):
    """A direction's backlog command: the hub runs it itself (minibacklog), in files of the Backlog.md format."""
    return minibacklog.run(args, path, capture)


def git_root(path):
    """The top of the git work tree that holds path, or Fail: a direction always works inside a real repo."""
    path = os.path.abspath(os.path.expanduser(path or ""))
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True) \
        if os.path.isdir(path) else None
    if not r or r.returncode != 0:
        raise Fail(i18n.t("directions.cli.not_git", path=path))
    return r.stdout.strip()


def meta_path(path):
    return os.path.join(path, "direction.json")


def load_meta(path):
    try:
        with open(meta_path(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_meta(path, meta):
    with open(meta_path(path) + ".tmp", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(meta_path(path) + ".tmp", meta_path(path))


def has_commit(repo):
    """A git folder with a commit — the daemon's own rule for a mini-hub's folder (hosts/worktree.folder)."""
    from hosts import worktree
    return worktree.folder(repo)[0] == "ok"


def repo_of(sess):
    """The git repo (with a commit) a session works in — the repo of its working copy, else its folder — for a direction
    it will lead; None: the direction gets a git folder of its own (own_repo). A session in a core copy gives the repo
    of its copy, not the copy the core removes later."""
    from hosts import worktree
    copy = (sess or {}).get("worktree")
    path = copy.get("repo") if isinstance(copy, dict) and copy.get("repo") else (sess or {}).get("cwd")
    return path if isinstance(path, str) and path and worktree.folder(path)[0] == "ok" else None


REPOS = "direction-repos"   # in the hub state folder: no session works there (workspaces/<name> is a session's own)


def own_repo(slug):
    """The direction's own git folder when no project repo is given (owner 06.11: a new direction starts in a git folder
    and is a mini-hub at once): <state>/direction-repos/<slug>, a new repo with an empty first commit — the core gives a
    direction a copy only of a repo with a commit (core/minihub.born). Only a new or empty folder: one left by an
    archived or renamed direction stays that direction's repo, the next free name (<slug>-2, …) is taken."""
    from core.daemon import default_state_dir
    base = os.path.abspath(os.path.join(default_state_dir(), REPOS))
    for n in range(1, 100):
        path = os.path.join(base, slug if n == 1 else f"{slug}-{n}")
        try:
            os.makedirs(path)
            break
        except FileExistsError:
            if os.path.isdir(path) and not os.listdir(path):
                break
        except OSError as e:   # a state folder that cannot be written: the command's own refusal, not a traceback
            raise Fail(i18n.t("directions.cli.own_repo_failed", path=path, error=e))
    else:
        raise Fail(i18n.t("directions.cli.own_repo_failed", path=base, error="no free folder name"))
    run = lambda *args, **kw: subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, **kw)
    who = {"GIT_AUTHOR_NAME": "Agents Hub", "GIT_AUTHOR_EMAIL": "hub@localhost",
           "GIT_COMMITTER_NAME": "Agents Hub", "GIT_COMMITTER_EMAIL": "hub@localhost"}
    steps = [("init", "-q"), ("mktree",), ("commit-tree", None, "-m", f"direction {slug}"), ("update-ref", "HEAD", None)]
    out = None
    for step in steps:   # the commit without the index: an empty tree, then the branch HEAD points to
        step = [x if x is not None else out.stdout.strip() for x in step]
        out = run(*step, input="" if step[0] == "mktree" else None, env={**os.environ, **who})
        if out.returncode != 0:
            shutil.rmtree(path, ignore_errors=True)
            raise Fail(i18n.t("directions.cli.own_repo_failed", path=path, error=(out.stderr or out.stdout).strip()[-300:]))
    return path


def new(slug, repo=None, adopt=False):
    check_slug(slug)
    made = None   # the direction's own repo made here: removed with the backlog if the backlog fails
    path = os.path.join(root(), slug)
    if os.path.exists(path):
        raise Fail(i18n.t("directions.cli.exists", slug=slug, path=path))
    if repo:
        repo = git_root(repo)
        if not has_commit(repo):   # its executors would get no copy, and the direction would work by itself for good
            raise Fail(i18n.t("directions.cli.no_commits", repo=repo, slug=slug))
    elif adopt:
        raise Fail(i18n.t("directions.cli.need_repo", slug=slug))
    else:
        repo = made = own_repo(slug)
    if adopt:  # the repo's ready Backlog.md (a personal hub, 03.10): the direction is a link to it, there is no second backlog
        own = os.path.join(repo, "backlog")
        if not os.path.isfile(os.path.join(own, "config.yml")):
            raise Fail(i18n.t("directions.cli.adopt_missing", repo=repo))
        os.makedirs(path)
        import platforms
        try:
            platforms.current.link_dir(own, os.path.join(path, "backlog"))
        except OSError as e:   # an empty folder would remain, and a retry would fail on "already exists"
            shutil.rmtree(path, ignore_errors=True)
            raise Fail(i18n.t("directions.cli.adopt_link_failed", own=own, error=e))
        save_meta(path, {"repo": repo, "adopted": True, "created": time.strftime("%Y-%m-%d %H:%M")})
        print(i18n.t("directions.cli.adopted", slug=slug, own=own))
        return 0
    os.makedirs(path)
    try:
        r = backlog(path, ["init", slug], capture=True)
    except OSError as e:   # read-only disk, permissions: an empty folder would remain, and a retry would fail on "already exists"
        for d in filter(None, (path, made)):
            shutil.rmtree(d, ignore_errors=True)
        raise Fail(i18n.t("directions.cli.init_error", error=e))
    if r.returncode != 0 or not os.path.isdir(os.path.join(path, "backlog")):
        for d in filter(None, (path, made)):
            shutil.rmtree(d, ignore_errors=True)
        raise Fail(i18n.t("directions.cli.init_failed", code=r.returncode, output=(r.stderr or r.stdout).strip()[-500:]))
    save_meta(path, {"repo": repo, "created": time.strftime("%Y-%m-%d %H:%M")})
    print(i18n.t("directions.cli.created_new", slug=slug, path=path, repo=repo))
    return 0


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def field(text, name):
    m = re.search(rf"^{name}:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", text, re.M)
    if m and m.group(1) in (">-", ">", "|-", "|"):   # a long value folded by YAML onto the following indented lines
        rest = re.match(r"((?:[ \t]+\S[^\n]*\n?)+)", text[m.end() + 1:])
        return " ".join(line.strip() for line in rest.group(1).splitlines()) if rest else None
    return m.group(1) if m else None


def task_files(path):
    return glob.glob(os.path.join(path, "backlog", "tasks", "*.md"))


def statuses(path):
    """The statuses of a direction's board (backlog/config.yml): the spelling a card's status is brought to."""
    return minibacklog.statuses_of(os.path.join(path, "backlog"))


def cards(path):
    """Cards of a direction's board (backlog/tasks; the archive is not on the board): [{id, status, title, mtime, updated}], for the
    facts layer (core/board.py)."""
    out, known = [], statuses(path)
    for f in task_files(path):
        try:
            text, mtime = read(f), os.path.getmtime(f)
        except OSError:
            continue
        head = text.partition("\n---\n")[0] if text.startswith("---\n") else ""
        out.append({"id": field(head, "id") or os.path.basename(f), "status": minibacklog.canon(field(head, "status"), known) or "?",
                    "title": field(head, "title") or os.path.basename(f), "mtime": mtime,
                    "updated": field(head, "updated_date")})
    return out


def hub_state():
    """Hub state (the state file + the journal tail) for board facts; None if there is no hub or it is unreadable."""
    try:
        from core import journal
        from core.daemon import default_state_dir
        state = default_state_dir()
        return journal.load_state(state) if os.path.isfile(os.path.join(state, "journal.jsonl")) else None
    except Exception:  # noqa: BLE001 — facts are a postscript to the output, not a reason to withhold it
        return None


def board_facts(path, st=None):
    """Core facts about a direction's board (core/board.py: who works, what is handed in and not reviewed); st is the hub state
    (read it by default). None if there is no hub or the state is unreadable: the backlog commands work without it too."""
    st = st if st is not None else hub_state()
    if st is None:
        return None
    try:
        from core import board
        return board.facts(st, os.path.basename(path), cards(path), time.time())
    except Exception:  # noqa: BLE001
        return None


def with_facts(text, facts):
    """The `task list` output with core facts: a card line "  TASK-N - title" + " · <fact>"; at the end, the ones without a card."""
    from core import board
    if not facts:
        return text
    lines = [line + (f" · {board.verdict(facts['cards'][m.group(1)])}"
                     if (m := re.match(r"\s+(\S+) - ", line)) and m.group(1) in facts["cards"] else "")
             for line in text.rstrip("\n").split("\n")]
    tail = [x for x in (board.unlinked_line(facts), board.lag_line(facts.get("lag"))) if x]
    return "\n".join(lines + tail) + "\n"


def run_link(slug, card, text):
    """The link of `hub task run` with a board card (core/board.py, spec dir-sync K1) for the session of direction slug: --board card
    (only for a direction and a card of its board, otherwise Fail) or the only TASK-N of the first line of the brief text, if
    such a card exists. {slug, card, how} or None."""
    from core import board
    from modules.directions import minibacklog
    if card is not None and not slug:
        raise Fail(i18n.t("directions.cli.board_only_direction"))
    cid = board.card_id(card) if card is not None else board.brief_card(text)
    if card is not None and not cid:
        raise Fail(i18n.t("directions.cli.board_bad_card", card=card))
    if not slug or not cid:
        return None
    try:
        minibacklog.Backlog(home(slug)).task_path(cid)
    except (Fail, minibacklog.Fail):
        if card is not None:
            raise Fail(i18n.t("directions.cli.board_no_card", card=cid, slug=slug)) from None
        return None
    return {"slug": slug, "card": cid, "how": "flag" if card is not None else "brief"}


def status_counts(path):
    counts, known = {}, statuses(path)
    for f in task_files(path):
        key = minibacklog.canon(field(read(f), "status"), known) or "?"
        counts[key] = counts.get(key, 0) + 1
    return counts


def list_():
    names = active()
    if not names:
        print(i18n.t("directions.cli.no_directions", root=root()))
        return 0
    from core import board
    st = hub_state()   # once for all directions
    for name in names:
        path = os.path.join(root(), name)
        counts = status_counts(path)
        changed = max((os.path.getmtime(p) for p in glob.glob(os.path.join(path, "backlog", "**"), recursive=True)),
                      default=os.path.getmtime(path))
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or i18n.t("directions.cli.no_tasks")
        repo = load_meta(path).get("repo") or i18n.t("directions.cli.repo_not_recorded")
        facts = (board_facts(path, st) if st is not None else None) or {}
        n = facts.get("counts") or {}
        known = "".join(f" · {label}: {n[k]}" for k, label in (("working", i18n.t("directions.cli.working")),
                                                                ("unsorted", i18n.t("directions.cli.unsorted")))
                        if n.get(k))
        known += f" · {board.lag_line(facts['lag'])}" if facts.get("lag") else ""
        print(i18n.t("directions.cli.list_line", name=name, summary=summary, known=known, repo=repo,
                     when=time.strftime('%Y-%m-%d %H:%M', time.localtime(changed))))
    return 0


def archive(slug):
    path = home(slug)
    dest_root = os.path.join(root(), "_archive")
    os.makedirs(dest_root, exist_ok=True)
    dest, n = os.path.join(dest_root, slug), 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(dest_root, f"{slug}-{n}")
    os.rename(path, dest)
    print(i18n.t("directions.cli.archived", slug=slug, dest=dest))
    return 0


def rename(slug, new):
    """A direction's new name: the backlog folder, the project name in it; a board of the old kind is stopped.
    The direction's session and its schedule jobs are renamed by the core: hub session rename (the hub or the owner)."""
    from modules.directions.boards import Boards
    path = home(slug)
    check_slug(new)
    dest = os.path.join(root(), new)
    if os.path.exists(dest):
        raise Fail(i18n.t("directions.cli.exists", slug=new, path=dest))
    boards = Boards(root())
    had_board = boards.alive(slug)
    if had_board:
        boards.stop(slug)  # the board holds the folder and shows the old name
    os.rename(path, dest)
    meta = load_meta(dest)
    save_meta(dest, {**meta, "was": (meta.get("was") or []) + [slug]})
    cfg = os.path.join(dest, "backlog", "config.yml")
    if os.path.isfile(cfg) and not meta.get("adopted"):   # the repo's backlog is its own, we leave the project name in it alone
        text = read(cfg)
        fixed = re.sub(r'^(project_name:\s*)(["\']?)' + re.escape(slug) + r'\2\s*$',
                       lambda m: f"{m.group(1)}{m.group(2)}{new}{m.group(2)}", text, count=1, flags=re.M)
        if fixed != text:
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(fixed)
    print(i18n.t("directions.cli.renamed", slug=slug, new=new, dest=dest))
    print(i18n.t("directions.cli.rename_session", slug=slug, new=new))
    return 0


def where(slug):
    repo = load_meta(home(slug)).get("repo")
    if not repo:
        raise Fail(i18n.t("directions.cli.repo_missing", slug=slug))
    print(repo)
    return 0


def set_repo(slug, repo):
    path = home(slug)
    meta = load_meta(path)
    meta["repo"] = git_root(repo)
    save_meta(path, meta)
    print(i18n.t("directions.cli.repo_set", slug=slug, repo=meta["repo"]))
    return 0


SECTIONS = ("context", "decision", "consequences")


def yaml_str(text):
    plain = re.fullmatch(r"[^:#'\"\[\]{},&*!|>%@`\n-][^:#'\"\n]*", text) and not text.endswith(" ")
    return text if plain else json.dumps(text, ensure_ascii=False)


def decide(slug, args):
    """Write the whole decision (context, decision, consequences) in one command, in the same file format."""
    path = home(slug)
    if not args or args[0].startswith("--"):
        raise Fail(i18n.t("directions.cli.decide_usage", slug=slug))
    title, body, rest = args[0].strip(), {}, args[1:]
    while rest:
        key = rest[0][2:] if rest[0].startswith("--") else ""
        if key not in SECTIONS or len(rest) < 2:
            raise Fail(i18n.t("directions.cli.decide_bad_arg", arg=repr(rest[0])))
        body[key], rest = rest[1].strip(), rest[2:]
    if not body.get("decision"):
        raise Fail(i18n.t("directions.cli.decide_empty"))
    folder = os.path.join(path, "backlog", "decisions")
    os.makedirs(folder, exist_ok=True)
    from modules.directions import minibacklog
    with minibacklog.writing(path):   # two decisions at once read the same highest number
        nums = [int(m.group(1)) for f in os.listdir(folder) if (m := re.match(r"decision-(\d+)", f))]
        n = max(nums, default=0) + 1
        name = re.sub(r"[\\/:*?\"<>|\s]+", "-", title).strip("-")[:80] or "decision"
        text = (f"---\nid: decision-{n}\ntitle: {yaml_str(title)}\ndate: '{time.strftime('%Y-%m-%d %H:%M')}'\n"
                f"status: accepted\n---\n" + "".join(f"## {k.capitalize()}\n\n{body.get(k, '')}\n\n" for k in SECTIONS))
        target = os.path.join(folder, f"decision-{n} - {name}.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    print(f"decision-{n}: {target}")
    return 0


def drop_decision(path, seq):
    """A `hub decided` record withdrawn (owner.decided_revoked): its file leaves decisions/, found by the [decided:seq]
    mark (a decision written by hand has none). Removed: the path; none: None."""
    folder = os.path.join(path, "backlog", "decisions")
    mark = f"[decided:{seq}]"
    from modules.directions import minibacklog
    with minibacklog.writing(path):
        for f in glob.glob(os.path.join(folder, "*.md")):
            text = read(f)
            if mark in text and "\nsource: hub-decided\n" in text:   # not a note that merely quotes the mark
                os.remove(f)
                return f
    return None


def record_decision(path, seq, text, by, ts):
    """A decision the direction's session or its executor took on its own (`hub decided`, event seq: reversible, without
    the owner), into the backlog's decisions/ in the same format as `decide`, with the status proposed (spec dir-sync K3). The [decided:seq] mark in the text: the same event
    does not create a file a second time. Written: the path; already there: None."""
    folder = os.path.join(path, "backlog", "decisions")
    if not os.path.isdir(os.path.join(path, "backlog")):
        raise Fail(f"no backlog {path}")   # the core's log (core/daemon.py board_decision), not a person
    mark = f"[decided:{seq}]"
    os.makedirs(folder, exist_ok=True)
    from modules.directions import minibacklog
    with minibacklog.writing(path):
        for f in glob.glob(os.path.join(folder, "*.md")):
            if mark in read(f):
                return None
        nums = [int(m.group(1)) for f in os.listdir(folder) if (m := re.match(r"decision-(\d+)", f))]
        n = max(nums, default=0) + 1
        title = " ".join(text.split())
        title = title if len(title) <= 80 else title[:79].rstrip() + "…"
        name = re.sub(r"[\\/:*?\"<>|\s]+", "-", title).strip("-")[:80] or "decision"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        # the file is the owner's to read (the direction's board): his language, whoever writes it
        body = {"context": i18n.tr(i18n.owner(), "directions.decision.recorded_context", by=by, when=when, mark=mark),
                "decision": text.strip(),
                "consequences": ""}
        target = os.path.join(folder, f"decision-{n} - {name}.md")
        with open(target + ".tmp", "w", encoding="utf-8") as f:
            f.write((f"---\nid: decision-{n}\ntitle: {yaml_str(title)}\ndate: '{when}'\nstatus: proposed\n"
                     f"source: hub-decided\n---\n" +
                     "".join(f"## {k.capitalize()}\n\n{body[k]}\n\n" for k in SECTIONS)).rstrip() + "\n")
        os.replace(target + ".tmp", target)
    return target


def untidy_decisions(base):
    """Decisions to clean up: all sections blank (the template `backlog decision create` leaves), or that template
    left above the real text (a heading repeated)."""
    out = []
    for f in glob.glob(os.path.join(base, "decisions", "*.md")):
        body = re.sub(r"\A---\n.*?\n---\n", "", read(f), flags=re.S)
        heads = re.findall(r"^## (\w+)", body, flags=re.M)
        if not re.sub(r"^#+ .*$|\s", "", body, flags=re.M) or len(heads) != len(set(heads)):
            out.append(f)
    return numbered(out)


def numbered(files):
    """decision-10 after decision-9: by the number in the file name, name as a tie-break."""
    def key(p):
        m = re.match(r"[a-z]+-(\d+)", os.path.basename(p))
        return (int(m.group(1)) if m else 0, os.path.basename(p))
    return sorted(files, key=key)


def resume(slug):
    path = home(slug)
    slug = os.path.basename(path)  # asked by the former name: show the current one
    base = os.path.join(path, "backlog")
    repo = load_meta(path).get("repo") or i18n.t("directions.cli.resume_repo_unset", slug=slug)
    out = [i18n.t("directions.cli.resume_title", slug=slug), i18n.t("directions.cli.resume_repo", repo=repo),
           i18n.t("directions.cli.resume_backlog", base=base, slug=slug), "",
           i18n.t("directions.cli.resume_docs")]
    docs = numbered(glob.glob(os.path.join(base, "docs", "*.md")))
    for f in docs:
        out += [f"### {os.path.basename(f)}", read(f).strip(), ""]
    if not docs:
        out += [i18n.t("directions.cli.resume_no_docs", slug=slug), ""]
    tasks = backlog(path, ["task", "list", "--plain", "--exclude-status", "Done"], capture=True)
    facts = board_facts(path)   # who works on the card, what is handed in and not reviewed, from the core journal
    listed = with_facts(tasks.stdout, facts) if tasks.returncode == 0 else tasks.stdout
    out += [i18n.t("directions.cli.resume_tasks"), (listed or tasks.stderr).strip() or i18n.t("directions.cli.no_tasks"),
            ""]
    known = statuses(path)
    for f in numbered(task_files(path)):
        text = read(f)
        if minibacklog.canon(field(text, "status"), known) == "In Progress" and field(text, "id"):
            view = backlog(path, ["task", "view", field(text, "id"), "--plain"], capture=True)
            out += [i18n.t("directions.cli.resume_in_progress", id=field(text, "id")), (view.stdout or view.stderr).strip(),
                    ""]
    # own decisions (`decide`) and those recorded by the core from `hub decided`: the last 5 of each, so the second kind does not push out the first
    every = numbered(glob.glob(os.path.join(base, "decisions", "*.md")))
    recorded = [f for f in every if field(read(f).partition("\n---\n")[0], "source") == "hub-decided"]
    decisions = [f for f in every if f not in recorded][-5:]
    out.append(i18n.t("directions.cli.resume_decisions"))
    for f in decisions:
        out += [f"### {os.path.basename(f)}", read(f).strip(), ""]
    if not decisions:
        out.append(i18n.t("directions.cli.no_decisions"))
    if recorded:
        out += ["", i18n.t("directions.cli.resume_recorded")]
        for f in recorded[-5:]:
            out += [f"### {os.path.basename(f)}", read(f).strip(), ""]
    untidy = untidy_decisions(base)
    if untidy:
        out += ["", i18n.t("directions.cli.resume_cleanup"), i18n.t("directions.cli.resume_cleanup_hint"),
                *[f"  {os.path.basename(f)}" for f in untidy]]
    print("\n".join(out))
    return 0


def create(path, args):
    """`... doc|task|... create`: the backlog output as is, with the id of what was created as the last line. It prints the id as the first
    line and the path as the last; a session takes `| tail -1` and got the path without the id (a session overwrote the "Goal" doc, 30.09)."""
    r = backlog(path, args, capture=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    m = re.search(r"^Created \w+ (\S+)", r.stdout, re.M)
    if r.returncode == 0 and m:
        print(i18n.t("directions.cli.created_id", id=m.group(1)))
    return r.returncode


COMMANDS = {"new": new, "resume": resume, "archive": archive, "where": where}


def board_cmd(argv):
    """A direction's board is on the hub page, without its own server (an embedded minimalist variant
    without servers): hub direction board <slug> is a link to it; --stop and boards stop and show boards of the old
    kind (a separate server on a port, from earlier hub versions), if any remain."""
    from modules.directions.boards import BoardError, Boards
    from modules.tailnet import access
    b = Boards(root())
    tail = access.status(b.state_dir)
    from core import instance
    port = instance.port("web", b.state_dir)
    try:
        if argv[0] == "boards":
            for x in b.running():
                print(i18n.t("directions.cli.board_legacy", slug=x["slug"], url=x["url"]))
            if not b.running():
                print(i18n.t("directions.cli.board_no_legacy"))
            return 0
        if len(argv) == 3 and argv[2] == "--stop":
            b.stop(check_slug(argv[1]))
            print(i18n.t("directions.cli.board_stopped", slug=argv[1]))
            return 0
        if len(argv) != 2:
            raise Fail("usage: hub direction board <slug> [--stop] | hub direction boards")
        slug = check_slug(argv[1])
        home(slug)   # no such direction: an error
        far = i18n.t("directions.cli.board_far", ip=tail["ip"], port=port, slug=slug) \
            if tail.get("ok") and tail.get("ip") else ""
        print(i18n.t("directions.cli.board_link", slug=slug, port=port, far=far))
        return 0
    except BoardError as e:
        raise Fail(str(e)) from None


def main(argv):
    try:
        if not argv:
            raise Fail(i18n.t(USAGE))
        if argv[0] == "list":
            return list_()
        if argv[0] in ("board", "boards"):
            return board_cmd(argv)
        if argv[0] == "new" and len(argv) >= 2:
            opts, rest = {}, argv[2:]
            while rest:   # --session is for the core (bin/hub): whose session leads the direction
                if rest[0] == "--adopt":
                    opts["adopt"], rest = True, rest[1:]
                elif rest[0] in ("--repo", "--session") and len(rest) >= 2:
                    opts[rest[0][2:]], rest = rest[1], rest[2:]
                else:
                    raise Fail(i18n.t(USAGE))
            return new(argv[1], opts.get("repo"), adopt=opts.get("adopt", False))
        if argv[0] == "lead" and argv[2:3] == ["--drop"]:   # the core takes the backlog off (bin/hub, task.backlog_dropped)
            if len(argv) != 4:
                raise Fail("usage: hub direction lead <slug> --drop <session>")
            home(argv[1])   # only the check: bin/hub sends the fact and says it when the core has accepted it
            return 0
        if argv[0] == "lead" and len(argv) in (2, 3):
            slug = os.path.basename(home(argv[1]))
            print(i18n.t("directions.cli.lead", slug=slug,
                         who=argv[2] if len(argv) == 3 else i18n.t("directions.cli.this_session")))
            return 0
        if argv[0] == "decide" and len(argv) >= 2:
            return decide(argv[1], argv[2:])
        if argv[0] == "repo" and len(argv) == 3:
            return set_repo(argv[1], argv[2])
        if argv[0] == "rename":
            if len(argv) != 3:
                raise Fail(i18n.t("directions.cli.rename_usage"))
            return rename(argv[1], argv[2])
        if argv[0] in COMMANDS and argv[0] != "new":
            if len(argv) != 2:
                raise Fail(i18n.t(USAGE))
            return COMMANDS[argv[0]](argv[1])
        if argv[0] in RESERVED:
            raise Fail(i18n.t(USAGE))
        if len(argv) >= 3 and argv[2] == "create":
            return create(home(argv[0]), argv[1:])
        if argv[1:3] == ["task", "list"]:   # with core facts per card (core/board.py)
            path = home(argv[0])
            r = backlog(path, argv[1:], capture=True)
            sys.stdout.write(with_facts(r.stdout, board_facts(path)) if r.returncode == 0 else r.stdout)
            sys.stderr.write(r.stderr)
            return r.returncode
        return backlog(home(argv[0]), argv[1:]).returncode
    except Fail as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
