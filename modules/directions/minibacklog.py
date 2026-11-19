"""A direction backlog that the hub keeps itself: the commands sessions actually call, in files of the Backlog.md format
(08.10: colleagues with a clean install lack the backlog utility, it needs node, and the direction was not created; since 16.10 the hub
does not call the utility at all, and reads old files as its own).

    init <name> [init flags]                   the backlog/ folder and config.yml
    task create "<title>" [-d …] [--ac …]… [-s …] [--notes …] [--plan …] [--priority …] [-l a,b]
    task edit <id> [-t …] [-d …] [-s …] [--ac …] [--check-ac N] [--uncheck-ac N] [--remove-ac N] [--notes …]
                   [--append-notes …] [--plan …] [--priority …] [-l …] [--add-label …]
    task list [-s <status>] [--exclude-status <status>]  ·  task view <id>  ·  task archive <id>
    doc create "<title>" [-t <type>] [--content …]  ·  doc update <id> [--content …] [--title …]  ·  doc list  ·  doc view <id>
    decision list  ·  decision view <id>       (to record a decision: hub direction decide)
    task show / doc show are the same as view; --help (on any command) and --version
"""
import glob, io, os, re, sys, time

from core import i18n

CAN = "task create|edit|list|view|archive, doc create|update|list|view, decision list|view"
HELP = "directions.cli.backlog_help"   # a catalog key ({can}): translated where it is printed
CONFIG = ('project_name: "{name}"\ndefault_status: "To Do"\nstatuses: ["To Do", "In Progress", "Done"]\nlabels: []\n'
          'date_format: yyyy-mm-dd\nmax_column_width: 20\nauto_open_browser: false\ndefault_port: 6420\n'
          'remote_operations: false\nauto_commit: false\nfilesystem_only: true\nbypass_git_hooks: false\n'
          'check_active_branches: false\nactive_branch_days: 30\ntask_prefix: "task"\n')
FOLDERS = ("tasks", "drafts", "completed", "archive/tasks", "archive/drafts", "docs", "decisions", "milestones")
# section: (heading, label, blank line after the heading), in the order Backlog.md writes them
SECTIONS = {"description": ("Description", "SECTION:DESCRIPTION", True), "ac": ("Acceptance Criteria", "AC", False),
            "plan": ("Implementation Plan", "SECTION:PLAN", True), "notes": ("Implementation Notes", "SECTION:NOTES", True)}
ALIASES = {"-d": "--description", "--desc": "--description", "-s": "--status", "-t": "--title", "-l": "--labels",
           "--label": "--labels", "--acceptance-criteria": "--ac", "-c": "--content"}
TAKES = {"create": {"--description", "--ac", "--status", "--notes", "--plan", "--priority", "--labels"},
         "edit": {"--title", "--description", "--status", "--ac", "--check-ac", "--uncheck-ac", "--remove-ac",
                  "--notes", "--append-notes", "--plan", "--priority", "--labels", "--add-label"},
         "list": {"--status", "--exclude-status"}, "view": set(), "archive": set(),
         "doc create": {"--type", "--title", "--content"}, "doc update": {"--content", "--title"}, "doc list": set(),
         "doc view": set()}


# which card the last command of this process touched: {card, fields}; bin/hub uses it to record the direction.card fact
# (core/board.py: who edited the card and when); None means no card was touched
last = None


class Fail(Exception):
    def __init__(self, text, code=2):
        super().__init__(text)
        self.code = code


def now():
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime())   # Backlog.md writes dates in UTC


def yaml_str(v):
    """As js-yaml does for Backlog.md: in single quotes, what would otherwise not be read as a string or not read whole."""
    risky = (not v or v != v.strip() or re.search(r":\s|:$|\s#", v) or v[0] in "-?:,[]{}#&*!|>'\"%@`"
             or re.fullmatch(r"[-+]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?|true|false|null|yes|no|on|off|~", v, re.I))
    return "'" + v.replace("'", "''") + "'" if risky else v


def file_title(title):
    t = re.sub(r"[/\\\"<>|?*:\s]", "-", title)
    t = re.sub(r"[,()!@+=\[\];%&'#{}~^$]", "", t)
    return re.sub(r"-+", "-", t).strip("-")[:100] or "task"


def parse(argv, cmd):
    """Positionals and {--option: [values]}; an option not on the command's list is refused (not silently skipped)."""
    pos, opts = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--plain":
            i += 1
            continue
        if a.startswith("-") and len(a) > 1 and not re.fullmatch(r"-\d+", a):
            name, eq, val = a.partition("=")
            name = "--type" if (name, cmd) == ("-t", "doc create") else ALIASES.get(name, name)
            if name not in TAKES[cmd]:
                raise Fail(i18n.t("directions.cli.backlog_unknown_option", opt=a, cmd=cmd,
                                  known=" ".join(sorted(TAKES[cmd])) or "—") + closest(name, cmd))
            if not eq:
                if i + 1 >= len(argv):
                    raise Fail(i18n.t("directions.cli.backlog_no_value", opt=a))
                i, val = i + 1, argv[i + 1]
            opts.setdefault(name, []).append(val)
        else:
            pos.append(a)
        i += 1
    return pos, opts


def closest(name, cmd):
    """What the session meant by an option the command does not take (13.11: --note, --notes-append, --append, --grep):
    the notes — which flag adds and which replaces; a search — there is none; otherwise the closest known flag."""
    from core import clihelp
    takes = TAKES[cmd]
    if ("note" in name or name == "--append") and "--append-notes" in takes:
        return "\n  " + i18n.t("directions.cli.backlog_notes_hint")
    if name == "--grep":
        return "\n  " + i18n.t("directions.cli.backlog_no_grep")
    if name.startswith("--append") and "--content" in takes:
        return "\n  " + i18n.t("directions.cli.backlog_doc_append")
    near = clihelp.near(name, sorted(takes))
    return "\n  " + i18n.t("directions.cli.backlog_closest", near=", ".join(near)) if near else ""


def valued(word):
    """An option word that takes the next one as its value (every option of the backlog does, unless `--x=value`)."""
    return word.startswith("-") and len(word) > 1 and "=" not in word and word not in ("--help", "-h", "--plain") \
        and not re.fullmatch(r"-\d+", word)


def one(opts, name):
    vals = opts.get(name)
    return vals[-1] if vals else None


def labels(vals):
    return [x.strip() for v in vals or [] for x in v.split(",") if x.strip()]


DEFAULT_STATUSES = ["To Do", "In Progress", "Done"]


def statuses_of(base):
    """The board's statuses from backlog/config.yml (base is the backlog folder); the default three if there is no list.
    The three default ones in any case read in their own spelling: the core counts "Done" and "In Progress" by it."""
    try:
        with open(os.path.join(base, "config.yml"), encoding="utf-8") as f:
            m = re.search(r"^statuses:\s*\[(.*)\]\s*$", f.read(), re.M)
    except OSError:
        m = None
    if not (m and m.group(1).strip()):
        return list(DEFAULT_STATUSES)
    got = [canon(x.strip().strip("'\""), DEFAULT_STATUSES) for x in m.group(1).split(",") if x.strip()]
    return list(dict.fromkeys(got))   # "Done" and "done" in one list are one column


def canon(status, statuses):
    """A status in the spelling of the list, whatever its case: "done" -> "Done". Not in the list - as written (a status of its own)."""
    status = (status or "").strip()
    return next((x for x in statuses if x.lower() == status.lower()), status)


class Backlog:
    def __init__(self, cwd):
        self.base = os.path.join(cwd, "backlog")

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def write(self, path, text):
        with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(path + ".tmp", path)

    def statuses(self):
        return statuses_of(self.base)

    def canon_status(self, status):
        return canon(status, self.statuses())

    # --- tasks
    def task_path(self, ref, folder="tasks"):
        m = re.fullmatch(r"(?:task-)?(\d+)", str(ref).strip(), re.I)
        found = m and glob.glob(os.path.join(self.base, folder, f"task-{int(m.group(1))} - *.md"))
        if not found:
            raise Fail(i18n.t("directions.cli.backlog_task_not_found", ref=ref, path=os.path.join(self.base, folder)), 1)
        return found[0]

    def next_id(self):
        nums = [int(m.group(1)) for f in glob.glob(os.path.join(self.base, "**", "task-*.md"), recursive=True)
                if (m := re.match(r"task-(\d+)", os.path.basename(f)))]
        return max(nums, default=0) + 1

    @staticmethod
    def split(text):
        if not text.startswith("---\n"):
            return [], text
        head, _, body = text[4:].partition("\n---\n")
        return head.split("\n"), body

    @staticmethod
    def front(head, key):
        for line in head:
            if line.startswith(key + ":"):
                v = line[len(key) + 1:].strip()
                return v[1:-1].replace("''", "'") if v[:1] == "'" and v[-1:] == "'" else v.strip('"')
        return None

    @staticmethod
    def set_front(head, key, lines, after=None):
        """key: value (one line or a block of lines "  - x"): replace in place or insert after `after`."""
        out, i, done = [], 0, False
        while i < len(head):
            if head[i].startswith(key + ":"):
                i += 1
                while i < len(head) and head[i].startswith("  "):
                    i += 1
                out += lines
                done = True
                continue
            out.append(head[i])
            if not done and after and head[i].startswith(after + ":"):
                j = i + 1
                while j < len(head) and head[j].startswith("  "):
                    out.append(head[j])
                    j += 1
                i = j
                if not any(h.startswith(key + ":") for h in head):
                    out += lines
                    done = True
                continue
            i += 1
        if not done:
            out += lines
        return out

    @staticmethod
    def section(body, key):
        _, mark, _ = SECTIONS[key]
        m = re.search(rf"<!-- {mark}:BEGIN -->\n(.*?)\n?<!-- {mark}:END -->", body, re.S)
        return m.group(1) if m else None

    @staticmethod
    def set_section(body, key, content):
        title, mark, gap = SECTIONS[key]
        block = f"## {title}\n{chr(10) if gap else ''}<!-- {mark}:BEGIN -->\n{content}\n<!-- {mark}:END -->"
        if re.search(rf"<!-- {mark}:BEGIN -->", body):
            return re.sub(rf"<!-- {mark}:BEGIN -->\n.*?<!-- {mark}:END -->",
                          lambda _: f"<!-- {mark}:BEGIN -->\n{content}\n<!-- {mark}:END -->", body, count=1, flags=re.S)
        keys = list(SECTIONS)
        for later in keys[keys.index(key) + 1:]:   # before the first section that comes after this one
            m = re.search(rf"^## {re.escape(SECTIONS[later][0])}\n", body, re.M)
            if m:
                return body[:m.start()] + block + "\n\n" + body[m.start():]
        return body.rstrip("\n") + ("\n\n" if body.strip() else "\n") + block + "\n"

    @staticmethod
    def criteria(body):
        text = Backlog.section(body, "ac") or ""
        return [(m.group(1) == "x", m.group(2)) for m in re.finditer(r"^- \[([ x])\] (?:#\d+ )?(.*)$", text, re.M)]

    @staticmethod
    def put_criteria(body, items):
        return Backlog.set_section(body, "ac", "\n".join(f"- [{'x' if d else ' '}] #{n} {t}"
                                                         for n, (d, t) in enumerate(items, 1)))

    def task_create(self, argv):
        pos, o = parse(argv, "create")
        if len(pos) != 1 or not pos[0].strip():
            raise Fail(i18n.t("directions.cli.backlog_create_usage"))
        title, n = pos[0].strip(), self.next_id()
        head = [f"id: TASK-{n}", f"title: {yaml_str(title)}", f"status: {self.canon_status(one(o, '--status')) or 'To Do'}",
                "assignee: []", f"created_date: '{now()}'"]
        head += (["labels:"] + [f"  - {yaml_str(x)}" for x in labels(o.get("--labels"))]) if labels(o.get("--labels")) \
            else ["labels: []"]
        head += ["dependencies: []"] + ([f"priority: {one(o, '--priority')}"] if one(o, "--priority") else [])
        head += [f"ordinal: {n * 1000}"]
        body = "\n"
        if one(o, "--description"):
            body = self.set_section(body, "description", one(o, "--description"))
        if o.get("--ac"):
            body = self.put_criteria(body, [(False, t) for t in o["--ac"]])
        for key in ("plan", "notes"):
            if one(o, f"--{key}"):
                body = self.set_section(body, key, one(o, f"--{key}"))
        os.makedirs(os.path.join(self.base, "tasks"), exist_ok=True)
        path = os.path.join(self.base, "tasks", f"task-{n} - {file_title(title)}.md")
        self.write(path, "---\n" + "\n".join(head) + "\n---\n" + ("\n" + body.lstrip("\n") if body.strip() else "\n\n"))
        print(f"Created task TASK-{n}\nFile: {path}")
        touched(f"TASK-{n}", ["create", *o])

    def task_edit(self, argv):
        pos, o = parse(argv, "edit")
        if len(pos) != 1:
            raise Fail("task edit <id> [--status …] [--append-notes …] …")
        path = self.task_path(pos[0])
        head, body = self.split(self.read(path))
        if one(o, "--title"):
            head = self.set_front(head, "title", [f"title: {yaml_str(one(o, '--title'))}"])
        if (one(o, "--status") or "").strip():   # blank: no status of its own, the card keeps its column
            head = self.set_front(head, "status", [f"status: {self.canon_status(one(o, '--status'))}"])
        if one(o, "--priority"):
            head = self.set_front(head, "priority", [f"priority: {one(o, '--priority')}"], after="dependencies")
        if "--labels" in o or "--add-label" in o:
            have = [] if "--labels" in o else [l.strip()[2:].strip("'") for l in head if l.startswith("  - ")
                                                and self._in_labels(head, l)]
            new = have + [x for x in labels(o.get("--labels")) + labels(o.get("--add-label")) if x not in have]
            head = self.set_front(head, "labels", ["labels:"] + [f"  - {yaml_str(x)}" for x in new] if new else ["labels: []"])
        head = self.set_front(head, "updated_date", [f"updated_date: '{now()}'"], after="created_date")
        if one(o, "--description") is not None:
            body = self.set_section(body, "description", one(o, "--description"))
        items = self.criteria(body)
        if any(k in o for k in ("--ac", "--check-ac", "--uncheck-ac", "--remove-ac")):
            for key, mark in (("--check-ac", True), ("--uncheck-ac", False)):
                for v in o.get(key, []):
                    k = self._index(v, items)
                    items[k] = (mark, items[k][1])
            drop = {self._index(v, items) for v in o.get("--remove-ac", [])}
            items = [x for k, x in enumerate(items) if k not in drop] + [(False, t) for t in o.get("--ac", [])]
            body = self.put_criteria(body, items)
        if one(o, "--plan") is not None:
            body = self.set_section(body, "plan", one(o, "--plan"))
        notes = one(o, "--notes") if "--notes" in o else self.section(body, "notes")
        if "--notes" in o or "--append-notes" in o:
            body = self.set_section(body, "notes", "\n\n".join([n for n in [notes] if n] + o.get("--append-notes", [])))
        self.write(path, "---\n" + "\n".join(head) + "\n---\n" + ("\n" + body.lstrip("\n") if body.strip() else "\n\n"))
        print(f"Updated task {self.front(head, 'id')}\nFile: {path}")
        touched(self.front(head, "id"), list(o))

    @staticmethod
    def _in_labels(head, line):
        i = head.index(line)
        while i > 0 and head[i].startswith("  "):
            i -= 1
        return head[i].startswith("labels:")

    @staticmethod
    def _index(v, items):
        if not re.fullmatch(r"\d+", v) or not 1 <= int(v) <= len(items):
            raise Fail(i18n.t("directions.cli.backlog_no_criterion", n=v, total=len(items)), 1)
        return int(v) - 1

    def tasks(self, folder="tasks"):
        out, known = [], self.statuses()
        for f in glob.glob(os.path.join(self.base, folder, "task-*.md")):
            head, body = self.split(self.read(f))
            out.append({"path": f, "head": head, "body": body, "id": self.front(head, "id") or "?",
                        "title": self.front(head, "title") or os.path.basename(f), "status": canon(self.front(head, "status"), known) or None,
                        "ordinal": float(self.front(head, "ordinal") or 1e9)})
        return sorted(out, key=lambda t: (t["ordinal"], int(re.sub(r"\D", "", t["id"]) or 0)))

    def task_list(self, argv):
        pos, o = parse(argv, "list")
        if pos:
            raise Fail(i18n.t("directions.cli.backlog_list_usage"))
        want, skip = [s.lower() for s in o.get("--status", [])], [s.lower() for s in o.get("--exclude-status", [])]
        tasks = [t for t in self.tasks() if (not want or (t["status"] or "").lower() in want)
                 and (t["status"] or "").lower() not in skip]
        known = self.statuses()
        groups = known + sorted({t["status"] or "?" for t in tasks} - set(known))
        blocks = [f"{g}:\n" + "\n".join(f"  {t['id']} - {t['title']}" for t in tasks if (t["status"] or "?") == g)
                  for g in groups if any((t["status"] or "?") == g for t in tasks)]
        print("\n\n".join(blocks) + "\n" if blocks else "No tasks found.")

    def task_view(self, argv):
        pos, _ = parse(argv, "view")
        if len(pos) != 1:
            raise Fail("task view <id>")
        path = self.task_path(pos[0])
        head, body = self.split(self.read(path))
        line = "-" * 50
        items = self.criteria(body)
        out = [f"File: {path}", "", f"Task {self.front(head, 'id')} - {self.front(head, 'title')}", "=" * 50, "",
               f"Status: {self.front(head, 'status')}"]
        out += [f"Priority: {self.front(head, 'priority')}"] if self.front(head, "priority") else []
        out += [f"Created: {self.front(head, 'created_date')} (UTC)"]
        out += [f"Updated: {self.front(head, 'updated_date')} (UTC)"] if self.front(head, "updated_date") else []
        out += ["", "Description:", line, (self.section(body, "description") or "No description provided"), "",
                "Acceptance Criteria:", line,
                "\n".join(f"- [{'x' if d else ' '}] #{n} {t}" for n, (d, t) in enumerate(items, 1))
                or "No acceptance criteria defined"]
        for key, name in (("plan", "Implementation Plan"), ("notes", "Implementation Notes")):
            if self.section(body, key):
                out += ["", f"{name}:", line, self.section(body, key)]
        print("\n".join(out))

    def task_archive(self, argv):
        pos, _ = parse(argv, "archive")
        if len(pos) != 1:
            raise Fail("task archive <id>")
        path = self.task_path(pos[0])
        os.makedirs(os.path.join(self.base, "archive", "tasks"), exist_ok=True)
        head, _ = self.split(self.read(path))
        os.replace(path, os.path.join(self.base, "archive", "tasks", os.path.basename(path)))
        print(f"Archived task {self.front(head, 'id')}")
        touched(self.front(head, "id"), ["archive"])

    # --- decisions (hub direction decide writes them; here we only read them)
    def decision_files(self):
        found = glob.glob(os.path.join(self.base, "decisions", "decision-*.md"))
        num = lambda f: int(m.group(1)) if (m := re.match(r"decision-(\d+)", os.path.basename(f))) else 0
        return sorted(found, key=lambda f: (num(f), f))

    def decision(self, argv):
        sub, rest = (argv[0], argv[1:]) if argv else ("", [])
        pos, _ = parse(rest, "view") if sub in ("list", "view", "show") else ([], {})
        files = self.decision_files()
        if sub == "list":
            if pos:
                raise Fail("decision list")
            for f in files:
                head, _ = self.split(self.read(f))
                print(f"{self.front(head, 'id') or os.path.basename(f)} - {self.front(head, 'title') or ''}")
            if not files:
                print(i18n.t("directions.cli.no_decisions"))
        elif sub in ("view", "show"):
            m = len(pos) == 1 and re.fullmatch(r"(?:decision-)?(\d+)", pos[0].strip(), re.I)
            found = [f for f in files if m and re.match(rf"decision-{int(m.group(1))} ", os.path.basename(f))]
            if not found:
                raise Fail(i18n.t("directions.cli.backlog_decision_not_found", ref=" ".join(pos) or "?",
                                  path=os.path.join(self.base, "decisions")), 1)
            print(self.read(found[0]))
        else:
            raise Fail(i18n.t("directions.cli.backlog_decision_usage"))

    # --- documents
    def doc_path(self, ref):
        m = re.fullmatch(r"(?:doc-)?(\d+)", str(ref).strip(), re.I)
        found = m and glob.glob(os.path.join(self.base, "docs", "**", f"doc-{int(m.group(1))} - *.md"), recursive=True)
        if not found:
            raise Fail(i18n.t("directions.cli.backlog_doc_not_found", ref=ref), 1)
        return found[0]

    def doc(self, argv):
        sub, rest = (argv[0], argv[1:]) if argv else ("", [])
        sub = "view" if sub == "show" else sub
        if f"doc {sub}" not in TAKES:
            raise Fail(i18n.t("directions.cli.backlog_doc_cannot", sub=sub, can=CAN))
        pos, o = parse(rest, f"doc {sub}")
        # a blank --content is a failed `$(cat …)`, not a text: update would wipe the document (17.11), create would leave
        # an empty one; refused before anything is read or written (no --content at all is still fine)
        if one(o, "--content") is not None and not one(o, "--content").strip():
            raise Fail(i18n.t("directions.cli.backlog_doc_empty", sub=sub))
        folder = os.path.join(self.base, "docs")
        if sub == "create":
            if len(pos) != 1:
                raise Fail(i18n.t("directions.cli.backlog_doc_create_usage"))
            nums = [int(m.group(1)) for f in glob.glob(os.path.join(folder, "**", "doc-*.md"), recursive=True)
                    if (m := re.match(r"doc-(\d+)", os.path.basename(f)))]
            n, title = max(nums, default=0) + 1, pos[0].strip()
            os.makedirs(folder, exist_ok=True)
            stamp = now()
            name = re.sub(r"[/\\\"<>|?*:]", "-", title).strip() or "doc"
            body = one(o, "--content")   # the text at once (13.11: sessions gave create -c / --content)
            self.write(os.path.join(folder, f"doc-{n} - {name}.md"),
                       f"---\nid: doc-{n}\ntitle: {yaml_str(title)}\ntype: {one(o, '--type') or 'other'}\n"
                       f"created_date: '{stamp}'\nupdated_date: '{stamp}'\n---\n"
                       + (body.rstrip("\n") + "\n" if body else ""))
            print(f"Created document doc-{n}\nPath: backlog/docs/doc-{n} - {name}.md")
        elif sub == "update":
            if len(pos) != 1:
                raise Fail("doc update <id> --content …")
            path = self.doc_path(pos[0])
            head, body = self.split(self.read(path))
            if one(o, "--title"):
                head = self.set_front(head, "title", [f"title: {yaml_str(one(o, '--title'))}"])
            head = self.set_front(head, "updated_date", [f"updated_date: '{now()}'"], after="created_date")
            if one(o, "--content") is not None:
                body = one(o, "--content").rstrip("\n") + "\n"
            self.write(path, "---\n" + "\n".join(head) + "\n---\n" + body)
            print(f"Updated document {self.front(head, 'id')}\nPath: {os.path.relpath(path, os.path.dirname(self.base))}")
        elif sub == "list":
            docs = sorted(glob.glob(os.path.join(folder, "**", "doc-*.md"), recursive=True),
                          key=lambda f: int(re.match(r"doc-(\d+)", os.path.basename(f)).group(1)))
            print("\n".join(f"{self.front(self.split(self.read(f))[0], 'id')} - "
                            f"{self.front(self.split(self.read(f))[0], 'title')}" for f in docs) or "No docs found.")
        else:
            if len(pos) != 1:
                raise Fail("doc view <id>")
            print(self.read(self.doc_path(pos[0])))


def touched(card, opts):
    global last
    last = {"card": card, "fields": sorted({o.lstrip("-") for o in opts})}


def init(cwd, argv):
    name = next((a for a in argv if not a.startswith("-")), None) or os.path.basename(cwd)
    base = os.path.join(cwd, "backlog")
    for d in FOLDERS:
        os.makedirs(os.path.join(base, d), exist_ok=True)
    cfg = os.path.join(base, "config.yml")
    if not os.path.exists(cfg):
        Backlog(cwd).write(cfg, CONFIG.format(name=name.replace('"', "")))
    print(i18n.t("directions.cli.backlog_initialized", name=name))


def lock_path(folder):
    """The write lock of a direction's backlog (tasks, documents, decisions: one at a time, the number is the highest on disk + 1).
    It lives in the hub folder, not in the direction folder: that can be a project repo (--adopt), and an extra file would land in git."""
    import hashlib
    from core.daemon import default_state_dir
    key = hashlib.sha1(os.path.realpath(folder).encode("utf-8", "surrogateescape")).hexdigest()[:12]
    d = os.path.join(default_state_dir(), "locks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"backlog-{key}.lock")


def writing(folder):
    """A `with` block in which only one process or thread writes the backlog of this folder."""
    from core import lock
    return lock.exclusive(lock_path(folder))


def main(argv, cwd):
    """The return code as the utility's: 0 is done, 1 no such task, 2 wrong command. One command at a time per
    direction: direction sessions call it simultaneously, and two creates read the same highest number."""
    with writing(cwd):
        return _main(argv, cwd)


def _main(argv, cwd):
    global last
    last = None
    try:
        if argv[:1] == ["--version"]:
            print(i18n.t("directions.cli.backlog_version"))
            return 0
        # --help after any words (13.11: `task edit TASK-7 --help` was an unknown option, its hint said --help
        # again), but not as an option's value (`--append-notes -h` is a note)
        if not argv or argv[0] in ("help", "--help", "-h") or any(
                a in ("--help", "-h") and not (i and valued(argv[i - 1])) for i, a in enumerate(argv)) \
                or argv in (["task"], ["doc"], ["decision"]):
            print(i18n.t(HELP, can=CAN))
            return 0
        if argv[:1] == ["init"]:
            init(cwd, argv[1:])
            return 0
        b = Backlog(cwd)
        if not os.path.isdir(b.base):
            raise Fail(i18n.t("directions.cli.backlog_missing", cwd=cwd), 1)
        if argv[:1] == ["task"] and len(argv) >= 2 and re.fullmatch(r"(?i)(task-)?\d+", argv[1]):
            argv = ["task", "view", *argv[1:]]          # `backlog task 3` is the same as view
        if argv[:2] == ["task", "show"]:
            argv = ["task", "view", *argv[2:]]
        if argv[:1] == ["task"] and len(argv) >= 2 and argv[1] in ("create", "edit", "list", "view", "archive"):
            getattr(b, f"task_{argv[1]}")(argv[2:])
            return 0
        if argv[:1] == ["decision"]:
            b.decision(argv[1:])
            return 0
        if argv[:1] == ["doc"]:
            b.doc(argv[1:])
            return 0
        raise Fail(i18n.t("directions.cli.backlog_unknown_command", cmd=" ".join(argv[:2]), can=CAN))
    except Fail as e:
        print(e, file=sys.stderr)
        return e.code


def run(argv, cwd, capture=False):
    """main() capturing the output, for direction.backlog(): the same CompletedProcess as the real utility returns."""
    import subprocess
    if not capture:
        return subprocess.CompletedProcess(argv, main(argv, cwd))
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(argv, cwd)
    finally:
        sys.stdout, sys.stderr = old
    return subprocess.CompletedProcess(argv, code, out.getvalue(), err.getvalue())
