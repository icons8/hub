"""Task record (owner decision 2026-10-04: a task is a hub entity, everyone present can write and read it, with control
against overwriting each other): the file tasks/<dN>.md in the hub folder.

- the header comes from the task (what, who set it, who does it); the "Task" section is edited with a fingerprint check:
  an edit passes only if the record has not changed since it was read (do not overwrite others' changes);
- the "Progress" log — lines "when · who: what", append-only: simultaneous writes do not break;
- appending and editing happen under one file lock (a header edit does not lose a line appended in the same second).
The core receives task.noted (who, task) — they make up the shared journal of tasks.
"""
import contextlib
import datetime
import functools
import hashlib
import os
import re

import platforms
from core import langdata, lock

TASK = "## Task\n"
LOG = "\n## Progress\n"
# A record is written in the hub's language (the owner reads it, sessions write to it in his language); the headings
# of the language come from the catalog, and a record keeps the ones it was made with. The records earlier cores wrote
# have the Russian headings (every hub's tasks/ keeps them): the pairs of every form are recognised.
_OLD = langdata.legacy("task_record")


def _lang(st):
    from core import i18n
    return i18n.state_lang(st) or i18n.SOURCE


def heads_of(lang):
    """The (task, log) headings of a language."""
    from core import i18n
    return ("## " + i18n.tr(lang, "core.taskrec.task") + "\n", "\n## " + i18n.tr(lang, "core.taskrec.log") + "\n")


@functools.lru_cache(maxsize=1)
def _known():   # once per process: the catalogs ship with the code
    from core import i18n
    pairs = [(TASK, LOG), (_OLD["task"], _OLD["log"])]
    for lang in i18n.available():
        try:
            pair = heads_of(lang)
        except Exception:  # noqa: BLE001 — a broken catalog: its headings are not recognised, the others are
            continue
        if pair not in pairs:
            pairs.append(pair)
    return tuple(pairs)


def _heads(text):
    """The (task, log) headings the record uses: the form whose "Task" heading comes first; none — the current one."""
    found = [(text.find(t), t, log) for t, log in _known() if t in text]
    both = [f for f in found if f[2] in text]   # the pair whose log heading is there too: two pairs may share a Task heading
    return min(both or found)[1:] if found else (TASK, LOG)


class Conflict(Exception):
    pass


def path(state_dir, did):
    return os.path.join(state_dir, "tasks", f"{did}.md")


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def resolve(st, ref, me=None, latest=False):
    """Task id by reference: dN; a session name — its latest unclosed task; no reference — one's own. latest (only
    hub task note): if there is no unclosed task of one's own — the latest of its tasks, even a closed one (20.10: a
    specialist works for days on the owner's direct messages after closing its task — there was nowhere to write the
    progress and decisions). show does not do this: one woken with a new task would read the old one until the new one
    lands in the state."""
    if ref and re.fullmatch(r"d\d+", ref) and ref in st["delegations"]:
        return ref
    who = ref if ref in st["sessions"] else (me if not ref else None)
    mine = [d for d in st["delegations"].values() if d["session"] == who]
    live = [d for d in mine if not closed(st, d["id"])] or (mine if latest and not ref else [])
    from core import delegations   # tasks made in the same millisecond tie: the later (the greater number) wins
    return max(live, key=delegations.born)["id"] if live else None


OPEN = ("working", "waiting-owner", "spec")   # the states of an open assignment; "replied" is the usual end of a task


def open_of(st, who):
    """The tasks a session is doing now (OPEN), the oldest first: which of them a note without a number would mean is a
    question only when there are several. A handed-in one (replied, stopped) is not a candidate."""
    from core import delegations
    return sorted((d for d in st["delegations"].values() if d["session"] == who and d["state"] in OPEN),
                  key=delegations.born)


def closed(st, did):
    return st["delegations"][did]["state"] == "done"


def may_write(st, did, me, outsider=False):
    """Everyone writes an open task; a closed one — those it belongs to: who did it, who set it, the hub and the owner
    (me=None); a module and a plugin (outsider) — no, whatever they call themselves."""
    d = st["delegations"][did]
    if not closed(st, did):
        return True
    return not outsider and (me is None or me in (d["session"], d.get("for_session"))
                             or (st["sessions"].get(me) or {}).get("kind") == "hub")


def brief(d):
    """The text of the task as its first message carried it — what head() writes into the "Task" section."""
    return (d.get("words") or d.get("context") or "").strip()


def _section(text):
    """Where the "Task" section of a record lies: (start, end) in text, whatever language its headings are in."""
    task, log = _heads(text)
    return text.index(task) + len(task), text.index(log)


def head(st, did):
    d = st["delegations"][did]
    text = brief(d)
    first = text.splitlines()[0][:100] if text else did
    from core import coretext, delegations, i18n
    lang = _lang(st)
    asked = d.get("for_session") or coretext.human(delegations.BY_OWNER if d.get("for") else delegations.BY_HUB, lang)
    task, log = heads_of(lang)
    return (f"# {did} — {first}\n\n" + i18n.tr(lang, "core.taskrec.set_by", asked=asked, session=d["session"]) + "\n\n"
            + task + text + "\n" + log)


BRIEF = ("(the task text is the first message of this task, unchanged; if you no longer have it — "
         "the whole text: hub task show {did} --full)")   # for a session: English


def for_doer(text, st, did):
    """The record as the one doing the task sees it: the "Task" section that is still the text of its first message (as
    head() wrote it) becomes one line — it already has the brief. An edited section (hub task head) or an archived
    one (add_context) is shown whole."""
    try:
        start, end = _section(text)
    except ValueError:   # a record without the usual sections
        return text
    section = text[start:end].strip()
    if not section or section != brief(st["delegations"][did]):
        return text
    return text[:start] + BRIEF.format(did=did) + "\n" + text[end:]


@contextlib.contextmanager
def _locked(state_dir, did):
    os.makedirs(os.path.join(state_dir, "tasks"), exist_ok=True)
    with lock.exclusive(path(state_dir, did) + ".lock"):
        yield


def _ensure(state_dir, st, did):
    p = path(state_dir, did)
    if not os.path.exists(p):
        # header first, then the whole file in one step: an empty or truncated record would stay without a header forever
        text = head(st, did)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            f.write(text)
        platforms.current.replace(p + ".tmp", p)


def read(state_dir, st, did):
    with _locked(state_dir, did):
        _ensure(state_dir, st, did)
        with open(path(state_dir, did), encoding="utf-8") as f:
            return f.read()


def note(state_dir, st, did, who, text, now=None):
    stamp = datetime.datetime.fromtimestamp(now or datetime.datetime.now().timestamp()).strftime("%d.%m %H:%M")
    line = f"- {stamp} · {who}: {' '.join(str(text).split())}\n"
    with _locked(state_dir, did):
        _ensure(state_dir, st, did)
        with open(path(state_dir, did), "a", encoding="utf-8") as f:
            f.write(line)
    return line


def set_task(state_dir, st, did, text, sha):
    """A new "Task" section if the record has not changed since fingerprint sha; otherwise Conflict."""
    with _locked(state_dir, did):
        _ensure(state_dir, st, did)
        p = path(state_dir, did)
        with open(p, encoding="utf-8") as f:
            cur = f.read()
        if fingerprint(cur) != sha:
            from core import i18n   # the CLI prints it to whoever runs it
            raise Conflict(i18n.t("core.taskrec.conflict"))
        start, end = _section(cur)
        new = cur[:start] + text.strip() + "\n" + cur[end:]
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            f.write(new)
        platforms.current.replace(p + ".tmp", p)
        return fingerprint(new)


def add_context(state_dir, st, did, text):
    """The full task text — verbatim at the end of the "Task" section (the progress log is untouched): before a task is
    archived (core.archived) the record must keep what the core will trim in the state. If already there — writes
    nothing. True — the text is in the record, re-read after writing (one lock per task: the first pass of a big hub is
    hundreds of records)."""
    with _locked(state_dir, did):
        _ensure(state_dir, st, did)
        p = path(state_dir, did)
        with open(p, encoding="utf-8") as f:
            cur = f.read()
        if text in cur:
            return True
        log = _heads(cur)[1]
        end = cur.index(log) if log in cur else len(cur)
        new = cur[:end].rstrip("\n") + "\n\n" + text + "\n" + cur[end:]
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            f.write(new)
        platforms.current.replace(p + ".tmp", p)
        with open(p, encoding="utf-8") as f:
            return text in f.read()
