"""Schedule: jobs live in the core journal (the SCHEDULE automaton, core/machines.py), not in files, and a single clock
process ticks (modules/scheduler), instead of launchd every minute (owner decision 2026-09-30).

Kinds: `at` (once at "YYYY-MM-DD HH:MM"), `every` (every 30m|2h|1d, no more often than 5 minutes), `cron` ("m h dom
mon dow"). A repeating job always ends: `until` defaults to 14 days, at most 30. A slot fires once; after downtime a
repeating job fires once for the last missed slot if it is no more than 6 hours old (the rest are skipped), a one-off
one always fires once, marked as late. Time is in the owner's time zone.

A command job (`command` instead of session + prompt; owner decision 2026-09-30: monitors go here, since a schedule
already exists): without until it lives indefinitely; when a slot arrives the core marks a run (run_slot), the clock
runs the command and reports schedule.ran with the exit code. The command writes to the owner itself — `hub notify`.

Precheck (`check` on a session job; 16.10 — watchers ticked by a session turn, and almost every turn ended with "nothing
new"): when a slot arrives the clock first runs the check command without a session (with a timeout, at most one run
per job) and reports schedule.due together with its code and output — one journal line per tick. Code 0 — nothing
new, the session sleeps, nothing in the feed; code 3 — a message to the session, the check output in the text; another
code or a timeout — a message with the check error: a check is not lost silently. A slot without a code (a clock of an
old version) wakes, like a job without a check. A job with a check may tick once a minute (without one, no more often
than once in 5 minutes).
"""
import datetime
import hashlib
import re
from core.zones import zone

from core import chain, coretext, forms, hubrun, langdata
from core.coretext import T
from core.guards import _sender_is_hub, checker, may_edit_job
from core.machines import FACTS, SCHEDULE, decide
from core.tools import Rejected

GRACE = 6 * 3600
KEEP_DONE = 7 * 86400  # removed, done and expired are visible for a week, then cleaned up
TIMING = ("kind", "at", "every", "cron")
ENDED = ("done", "expired", "removed")
DEFAULT_DAYS = 14
MAX_DAYS = 30
MIN_EVERY = 300
MIN_EVERY_CHECK = 60   # with a precheck a tick without news costs a script run, not a session turn (decision 16.10)
CHECK_NEW = 3   # the precheck code for "there is news" (0 — nothing new, another — a check error)
CHECK_OUT = 20000   # characters of precheck output in the event and text: the rest is cut, the full one is in a file
CHECK_MAX = 4000   # characters in the precheck command itself
CHECK_TIMEOUT_MAX = 900   # seconds: the longest a job may let its precheck run (`check_timeout`; by default the clock's 2 minutes)
# A milestone is a command job with this id prefix (the rule to sessions, hosts/streamjson.PROGRESS): `hub notify` from the
# job's command tells the owner how a long work goes. The core makes it reliable (a check of a week of them, 06.11):
# the first notice at once, and a job whose notices stand still is removed
PROGRESS_PREFIX = "progress-"
MILESTONE_LIMIT = 4   # notices in a row with the same text, all saying "no data", or runs with no notice: the job is removed (legitimate jobs: <= 3)
# the last notice (a hash of its whole text), the counts in a row, the run slot of the last notice, the runs with none in a row
MILESTONE_KEYS = ("note_sig", "note_same", "note_empty", "note_slot", "note_none")
# "no data" as words, in the languages a source-less milestone speaks (the others: language data, core/langdata.py)
NO_DATA = re.compile("(?<!\\w)(?:" + langdata.alt("words", "no_data") + "|no data)(?!\\w)", re.I)
NO_DATA_LABEL = 4   # words (numbers too) around the phrase that still make it the whole notice ("gen 3: no data (2)"); more — a line with other news
TASK_OPEN = ("working", "waiting-owner")   # a task handed out and not handed in (core/machines.py DELEGATION)
MILESTONE_ROOTS = 5   # the chain roots of the turns that set a milestone, kept on it (the last ones): the work it tells of
WAIT_ID = re.compile(r"^(.+-wait-[0-9a-f]+)-\d+$")   # checks of one `hub wait` (bin/hub): <session>-wait-<label>-<n>
WAIT_WHAT = forms.WAIT_WHAT   # the text of a `hub wait` check (bin/hub) — for jobs without the wait field
UNITS = {"m": 60, "h": 3600, "d": 86400}
BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class Invalid(ValueError):
    """What is wrong with a job: str() gives the reason itself, with the marks of the core's phrases in it (a refusal
    shows in the owner's language, core/coretext.py)."""
    def __str__(self):
        return self.args[0] if len(self.args) == 1 and isinstance(self.args[0], str) else super().__str__()


# --- cron -------------------------------------------------------------------------------------------------------

def _field(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        step = int(step) if step else 1
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = b = int(rng)
            if step > 1:
                b = hi
        if not (lo <= a <= b <= hi) or step < 1:
            raise Invalid(T("core.schedule.cron_range", "cron: {part} outside {lo}-{hi}").format(part=repr(part), lo=lo,
                                                                                                  hi=hi))
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr):
    f = str(expr).split()
    if len(f) != 5:
        raise Invalid(T("core.schedule.cron_fields", "cron: 5 fields are needed — minute hour day month weekday"))
    try:
        sets = [_field(s, lo, hi) for s, (lo, hi) in zip(f, BOUNDS)]
    except ValueError as e:
        raise Invalid(str(e)) from None
    if 7 in sets[4]:
        sets[4] = (sets[4] - {7}) | {0}
    return sets, f[2] != "*", f[4] != "*"


def matcher(expr):
    """A minute check for a cron expression (parsed once)."""
    (mins, hours, doms, mons, dows), dom_set, dow_set = parse_cron(expr)

    def day_ok(dt):
        dom, dow = dt.day in doms, (dt.weekday() + 1) % 7 in dows
        return (dom or dow) if dom_set and dow_set else (dom and dow)  # classic cron: if both are set, either one

    def match(dt):
        if dt.minute not in mins or dt.hour not in hours or dt.month not in mons:
            return False
        return day_ok(dt)
    match.sets, match.day_ok = (mins, hours, mons), day_ok
    return match


def cron_match(expr, dt):
    return matcher(expr)(dt)


def _local(ts, tz):
    return datetime.datetime.fromtimestamp(ts, zone(tz))


def cron_next(expr, after, tz, until=None):
    """The first minute after `after` that fits the expression, no later than until (a year by default). Not minute by
    minute: a month, day or hour that does not fit is skipped whole (07.10: iterating minutes took 80% of the page
    snapshot time). Steps follow the wall clock, as the iteration did before: on a clock-change night the answer is the
    same. A wall minute of a job with given hours is counted at its first moment, so in the repeated hour the answer is
    not earlier than `after` (slot_due fires once for each wall minute); an every-hour job runs on real minutes, in the
    repeated hour too."""
    match = matcher(expr)
    mins, hours, mons = match.sets
    one = datetime.timedelta(minutes=1)
    dt = _local(after, tz).replace(second=0, microsecond=0) + one
    end = until if until is not None else after + 366 * 86400
    found = None
    while dt.timestamp() <= end:
        if dt.month not in mons:   # to the first of the next month
            y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
            dt = dt.replace(year=y, month=m, day=1, hour=0, minute=0)
        elif not match.day_ok(dt):   # to the start of the next day
            dt = dt.replace(hour=0, minute=0) + datetime.timedelta(days=1)
        elif dt.hour not in hours:   # to the start of the next hour
            dt = dt.replace(minute=0) + datetime.timedelta(hours=1)
        elif dt.minute not in mins or dt.timestamp() <= after:   # the second pass of the repeated hour is already past
            dt += one
        else:
            found = dt.timestamp()
            break
    if every_hour(match):
        # an every-hour job runs in the second pass of the repeated hour too: the real minute is earlier than the
        # wall answer. Such a minute is no further than the length of a repeated hour from `after` and occurs only
        # around a clock change
        stop = min(found if found is not None else end, after + REPEAT)
        if _local(after, tz).fold or _local(after, tz).utcoffset() != _local(stop, tz).utcoffset():
            at = after // 60 * 60 + 60
            while at < stop:
                if match(_local(at, tz)):
                    return at
                at += 60
    return found


REPEAT = 2 * 3600   # longer than any repeated hour in the zone database (an hour; half an hour on Lord Howe Island)


def every_hour(match):
    """The hour field is every hour: the job runs on real minutes, not once per wall minute."""
    return len(match.sets[1]) == 24


# --- job ----------------------------------------------------------------------------------------------------------

def parse_every(text, least=MIN_EVERY):
    m = re.fullmatch(r"(\d+)\s*([mhd])", str(text).strip())
    if not m or int(m.group(1)) == 0:
        raise Invalid("every: 30m, 2h, 1d …")
    return _every_secs(int(m.group(1)) * UNITS[m.group(2)], least)


def _every_secs(value, least=MIN_EVERY):
    """every as a number of seconds (JSON, `hub schedule apply`): the same limit as for text."""
    try:
        secs = int(value)
    except (TypeError, ValueError, OverflowError):
        raise Invalid("every: 30m, 2h, 1d …") from None
    if secs < least:
        raise Invalid(T("core.schedule.every_minute", "every: no more often than once a minute") if least <= 60 else
                      T("core.schedule.every_least", "every: no more often than once in {minutes} minutes (with --check — "
                        "once a minute)").format(minutes=least // 60))
    return secs


def parse_time(text, tz):
    try:
        dt = datetime.datetime.strptime(str(text).strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise Invalid(T("core.schedule.time_form", "time: YYYY-MM-DD HH:MM")) from None
    return dt.replace(tzinfo=zone(tz)).timestamp()


def check_timeout(value):
    """A job's precheck time limit in seconds: a whole number from 1 to CHECK_TIMEOUT_MAX (a number, or its text from the command line)."""
    try:
        if isinstance(value, bool) or isinstance(value, float) and value != int(value):
            raise ValueError
        secs = int(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError, OverflowError, AttributeError):
        secs = 0
    if not 1 <= secs <= CHECK_TIMEOUT_MAX:
        raise Invalid(T("core.schedule.check_timeout_range", "check_timeout: whole seconds from 1 to {max}").format(max=CHECK_TIMEOUT_MAX))
    return secs


def normalize(spec, now, tz):
    """A job from what a person or the hub wrote -> a validated record for the journal. Invalid says what is wrong."""
    job = {"id": str(spec.get("id") or "").strip(), "session": str(spec.get("session") or "").strip(),
           "prompt": str(spec.get("prompt") or "").strip(), "command": str(spec.get("command") or "").strip(),
           "check": str(spec.get("check") or "").strip()}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", job["id"]):
        raise Invalid(T("core.schedule.bad_id", "id: Latin letters, digits, dot, hyphen, up to 64 characters"))
    if job["command"] and job["prompt"]:
        raise Invalid(T("core.schedule.command_or_prompt", "a job is either a command (command) or a message to a session "
                        "(session + prompt), not both"))
    if not job["command"] and not job["session"]:
        raise Invalid(T("core.schedule.no_session", "session: whom to send it to"))
    if not job["command"] and not job["prompt"]:
        raise Invalid(T("core.schedule.no_prompt", "prompt: empty"))
    if job["check"] and job["command"]:
        raise Invalid(T("core.schedule.check_of_command", "check is the precheck of a session job (session + prompt), a "
                        "command job has none"))
    if len(job["check"]) > CHECK_MAX:
        raise Invalid(T("core.schedule.check_long", "check: longer than {max} characters").format(max=CHECK_MAX))
    if not job["check"]:
        del job["check"]
    if spec.get("check_timeout") not in (None, ""):
        if not job.get("check"):
            raise Invalid(T("core.schedule.check_timeout_no_check", "check_timeout: only with a check"))
        job["check_timeout"] = check_timeout(spec["check_timeout"])
    if spec.get("wait") and WAIT_ID.match(job["id"]) and not job["command"]:
        job["wait"] = str(spec["wait"]).strip()[:CHECK_MAX]   # a `hub wait` check: what we wait for is a field (bin/hub)
    limit = now + MAX_DAYS * 86400
    kinds = [k for k in ("at", "every", "cron") if spec.get(k)]
    if len(kinds) != 1:
        raise Invalid(T("core.schedule.one_timing", "exactly one of these is needed: at, every, cron"))
    if kinds == ["at"]:
        at = parse_time(spec["at"], tz) if isinstance(spec["at"], str) else float(spec["at"])
        if at < now - 60:
            raise Invalid(T("core.schedule.at_past", "at: the time has passed — it is {now} ({tz}) now, the job's time is "
                            "in this zone").format(now=_local(now, tz).strftime('%Y-%m-%d %H:%M'), tz=tz))
        if at > limit:
            raise Invalid(T("core.schedule.at_far", "at: further than {max} days").format(max=MAX_DAYS))
        job.update(kind="at", at=at, until=at)
        return job
    until = spec.get("until")
    until = (parse_time(until + " 23:59", tz) if isinstance(until, str) else float(until)) if until else \
        None if job["command"] else now + DEFAULT_DAYS * 86400  # a monitor lives until removed
    if until is not None and until <= now:
        raise Invalid(T("core.schedule.until_past", "until: already passed"))
    if until is not None and until > limit + 86400:
        raise Invalid(T("core.schedule.until_far", "until: further than {max} days").format(max=MAX_DAYS) + (
            " " + T("core.schedule.until_far_command", "(without until a command repeats indefinitely)")
            if job["command"] else ""))
    job["until"] = until
    if kinds == ["cron"]:
        parse_cron(spec["cron"])
        job.update(kind="cron", cron=str(spec["cron"]).strip())
    else:
        least = MIN_EVERY_CHECK if job.get("check") else MIN_EVERY
        every = parse_every(spec["every"], least) if isinstance(spec["every"], str) else _every_secs(spec["every"], least)
        job.update(kind="every", every=every, start=float(spec.get("start") or now + every))
    return job


def waiting_for(job):
    """What a `hub wait` check waits for — for the feed ("waiting for an answer — <what>" without the service
    instruction): the wait field, for old jobs — from the text (both forms in core/forms.py); not a check or a text
    of another form — None (tests/test_wait.py compares it with what bin/hub writes)."""
    if not WAIT_ID.match(job.get("id") or ""):
        return None
    if isinstance(job.get("wait"), str) and job["wait"]:
        return job["wait"]
    m = WAIT_WHAT.match(job.get("prompt") or "")
    return m[1] if m else None


def kin(jid):
    """A "same thing" key for copies in the queue: for checks of one wait, the wait itself; for the rest, the job id."""
    m = WAIT_ID.match(jid or "")
    return m[1] if m else jid


def shell(cfg):
    """hub.json `schedule.shell`: the shell of command jobs and pre-checks (platforms job_shell: "bash" is Git Bash on
    Windows), None when not set or the section is not an object."""
    section = (cfg or {}).get("schedule")
    return section.get("shell") if isinstance(section, dict) else None


def end(job):
    """A job's deadline; a command without until is indefinite."""
    return float("inf") if job.get("until") is None else job["until"]


def slot_due(job, now, tz):
    """The last slot ≤ now for which the job has not fired yet, or None."""
    last = job.get("last_slot") or 0
    if job["kind"] == "at":
        return job["at"] if not last and job["at"] <= now else None
    if job["kind"] == "every":
        if now < job["start"]:
            return None
        slot = job["start"] + (now - job["start"]) // job["every"] * job["every"]
        return slot if slot > last and slot <= end(job) else None
    # Wall minutes, each at its first moment (fold=0), as cron_next counts them: in the repeated hour a minute of a job
    # with given hours fires once, not a second time on the second pass (an every-hour job runs on real minutes); a
    # minute skipped by a clock change goes past the gap and does not arrive until that moment (the core rejects a
    # slot in the future)
    match = matcher(job["cron"])
    floor = max(last, job.get("created", 0) - 60, now - GRACE - 60)
    if every_hour(match):   # on real minutes: the repeated hour runs once more (cron_next)
        at = now // 60 * 60
        while at > floor:
            if at <= end(job) and match(_local(at, tz)):
                return at
            at -= 60
        return None
    minute = _local(now, tz).replace(second=0, microsecond=0, fold=0)
    while minute.timestamp() > floor:
        at = minute.timestamp()
        if at <= now and at <= end(job) and match(minute):
            return at
        minute -= datetime.timedelta(minutes=1)
    return None


def next_due(job, now, tz):
    """The next slot after now (None — it will not fire again). For display and for the clock's sleep."""
    if job.get("state") not in (None, "active"):
        return None
    if job["kind"] == "at":
        return None if job.get("last_slot") else job["at"]
    if job["kind"] == "every":
        due = job["start"] if now < job["start"] else \
            job["start"] + ((now - job["start"]) // job["every"] + 1) * job["every"]
    else:
        due = cron_next(job["cron"], now, tz, until=job.get("until"))
    return due if due and due <= end(job) else None


UPCOMING = 5  # how many upcoming runs to show


def upcoming(job, now, tz, n=UPCOMING):
    """The next n runs (for display: "when the other runs will be", owner request 2026-10-01)."""
    out, t = [], now
    while len(out) < n:
        due = next_due(job, t, tz)
        if due is None or (out and due <= out[-1]):
            break
        out.append(due)
        if job["kind"] == "at":
            break
        t = due
    return out


GROUP_PREFIX = 160


def groups(jobs):
    """{id: id of the group's first job}. The same task under different jobs (once + every week, two one-offs): the same
    addressee, and one text is the beginning of the other or the first GROUP_PREFIX characters match."""
    text = lambda j: " ".join((j.get("command") or j.get("prompt") or "").split())  # noqa: E731
    same = lambda a, b: a.startswith(b) or b.startswith(a) or a[:GROUP_PREFIX] == b[:GROUP_PREFIX]  # noqa: E731
    # each job's text once, heads by addressee and kind: the snapshot groups the active and paused jobs on every build
    heads, out = {}, {}
    for j in jobs:
        mine = text(j)
        bucket = heads.setdefault((j.get("session"), bool(j.get("command"))), [])
        head = next((h for h, words in bucket if same(words, mine)), None)
        if head is None:
            bucket.append((j, mine))
        out[j["id"]] = (head or j)["id"]
    return out


# cron weekday -> catalog key (7 is Sunday too)
DAYS = {"0": "core.schedule.day.0", "1": "core.schedule.day.1", "2": "core.schedule.day.2", "3": "core.schedule.day.3",
        "4": "core.schedule.day.4", "5": "core.schedule.day.5", "6": "core.schedule.day.6", "7": "core.schedule.day.0"}
WEEK = {"*": "core.schedule.day.every", "1-5": "core.schedule.day.weekdays", "0,6": "core.schedule.day.weekends",
        "6,0": "core.schedule.day.weekends", "6,7": "core.schedule.day.weekends"}


def human_cron(expr, lang=None):
    """"When" in words (owner decision 2026-10-03: the schedule is a readable overview): frequent cron forms; the rest
    as is. lang is the text language (the process language by default, core/i18n.py)."""
    from core import i18n
    tr = lambda key, **kw: i18n.tr(lang or i18n.current(), key, **kw)  # noqa: E731
    raw = tr("core.schedule.cron_raw", expr=expr)
    parts = str(expr).split()
    if len(parts) != 5:
        return raw
    m, h, dom, mon, dow = parts
    if dom != "*" or mon != "*":
        return raw
    num = lambda x: x.isdigit()  # noqa: E731
    if m.startswith("*/") and num(m[2:]) and h == "*" and dow == "*":
        return tr("core.schedule.every_min", n=int(m[2:]))
    if num(m) and h.startswith("*/") and num(h[2:]) and dow == "*":
        return tr("core.schedule.every_hours_at", n=int(h[2:]), m=f"{int(m):02d}") if int(m) \
            else tr("core.schedule.every_hours", n=int(h[2:]))
    if num(m) and h == "*" and dow == "*":
        return tr("core.schedule.hourly_at", m=f"{int(m):02d}") if int(m) else tr("core.schedule.hourly")
    key = WEEK.get(dow) or DAYS.get(dow)
    if not key or not num(m):
        return raw
    day = tr(key)
    if all(num(x) for x in h.split(",")):
        times = [f"{int(x):02d}:{int(m):02d}" for x in h.split(",")]
        joined = tr("core.schedule.and", a=times[0], b=times[1]) if len(times) == 2 else ", ".join(times)
        return tr("core.schedule.day_at", day=day, times=joined)
    if "-" in h and all(num(x) for x in h.split("-")) and int(m) == 0:
        a, b = h.split("-")
        return tr("core.schedule.day_hours", day=day, a=int(a), b=int(b))
    return raw


def when_text(job, tz, lang=None):
    """When the job fires, in words. lang is the text language: display (views) uses the process language; a text the
    core puts into the journal is explicit (the reducer does not depend on the process language). lang "phrase" —
    English with the marks of its catalog phrases: a core message the owner reads in his language."""
    from core import i18n
    tr = coretext.catalog if lang == "phrase" else \
        (lambda key, **kw: i18n.tr(lang or i18n.current(), key, **kw))  # noqa: E731
    fmt = lambda ts: _local(ts, tz).strftime("%d.%m %H:%M")  # noqa: E731
    if job["kind"] == "at":
        return tr("core.schedule.once", when=fmt(job["at"]))
    until = tr("core.schedule.indefinitely") if job.get("until") is None else \
        tr("core.schedule.until", date=_local(job["until"], tz).strftime("%d.%m"))
    if job["kind"] == "every":
        s = job["every"]
        if s % 86400 == 0:
            n = s // 86400
            return (tr("core.schedule.every_day") if n == 1 else tr("core.schedule.every_days", count=n)) + " " + until
        if s % 3600 == 0:
            n = s // 3600
            return (tr("core.schedule.hourly") if n == 1 else tr("core.schedule.every_hours", n=n)) + " " + until
        return tr("core.schedule.every_min", n=s // 60) + " " + until
    # a cron line has no catalog phrase of its own: English when the text is a phrase (until keeps its marks)
    return human_cron(job["cron"], i18n.SOURCE if lang == "phrase" else lang) + " " + until


# --- in the core: schedule events --------------------------------------------------------------------------------

def owner_of(st, job):
    """Whose job it is (like jobOf on the page): a session job belongs to the one it goes to (session); a command to
    the session it was handed to (`hub schedule retarget` writes session), otherwise to the one that set it (by); if
    its owner is removed — nobody's (""). The clock runs a command without a session: it wakes one only by running
    `hub session wake <name>` (wakes), and that one need not be its owner."""
    if not job.get("command"):
        return job.get("session") or ""
    who = job.get("session") or job.get("by") or ""
    return who if (st["sessions"].get(who) or {}).get("state") not in (None, "removed") else ""


# the note on a handed `hub wait` check: in the job's prompt (state), so the check sees it there; earlier cores wrote it
# in Russian — a prompt with that note does not get a second one
HANDED = "\n\n(The check was set by executor "
HANDED_OLD = langdata.legacy("schedule_handed")


def hand_waits(st, name, raiser, ts):
    """Executor name was removed from the list or crashed — hand its `hub wait` checks (it waited for an answer) to the
    one who brought it up (raiser — a live name or None): that one carries the task on; if there is no raiser — remove
    them. They do not go to the hub as "no such session — decide whom to give it to" (22.10: five checks of removed
    executors in two days, retarget of a fired one answered "already fired"). Other jobs are as for any session. The
    note is single and names the one who set the check (by id), also when the check is passed further."""
    for job in st.get("schedule", {}).values():
        if job["state"] != "active" or job.get("session") != name or job.get("command") or not WAIT_ID.match(job["id"]):
            continue
        if raiser:
            job["session"] = raiser
            if HANDED not in job["prompt"] and HANDED_OLD not in job["prompt"]:
                job["prompt"] += f"{HANDED}{kin(job['id']).rsplit('-wait-', 1)[0]} — it was removed from the list or " \
                                 "crashed; you are the one waiting for this answer now.)"
        else:
            job["state"], job["since"] = "removed", ts


def wakes(job, name):
    """The job wakes session name (its "check"): a session job addressed to it does; a command does only when it runs
    `hub session wake <name>` (the whole name), not merely because it was handed to it."""
    if job.get("command"):   # the name first: wakes() is asked for every session × job on each tick
        return name in job["command"] and bool(re.search(r"\bsession\s+wake\s+['\"]?" + re.escape(name) + r"(?![\w.-])", job["command"]))
    return job.get("session") == name


WHO = langdata.legacy("who")   # the job's `by` for the core and the owner, as earlier cores wrote and compare it (the
# page gets codes: views.schedule_view)


def _voice(data):
    """command_by of a set with a command: who wrote that text — a session, a plugin or module process, or the owner
    (the core's own jobs too)."""
    if data.get("channel") == "session" and data.get("sender"):
        return {"session": str(data["sender"])}
    if data.get("channel") == "plugin" and data.get("sender"):
        return {"plugin": forms.outside_id(data["sender"])}
    return {"owner": True}


def speaker_of(job):
    """Whose word a command job's command is: ("session", name), ("plugin", "ext:<n>" | "mod:<n>") or ("owner", None).
    The clock runs it with that name (modules/scheduler/clock.py): a command a session wrote passes the gates that
    session passes, not the owner's terminal (18.11, a security review: a plain session approved its own risky tool
    and could turn the hub's password off through a job). A job of an older core has no command_by — its setter (by)."""
    who = job.get("command_by")
    if isinstance(who, dict):
        for kind in ("session", "plugin"):
            if isinstance(who.get(kind), str) and who[kind]:
                return kind, who[kind]
        return "owner", None
    by = job.get("by")
    if not isinstance(by, str) or not by or by in (WHO["owner"], WHO["core"]):
        return "owner", None
    return "session", by


def from_own_job(st, data):
    """A session's fact comes from the command of its own command job (schedule_job of a job it wrote): `hub notify`
    stays open to it and its messages keep the schedule's label, as when the clock ran every command as the owner. A
    session's turn and its job's pre-check are not (no command, or another's)."""
    jid = data.get("schedule_job")
    job = (st.get("schedule") or {}).get(jid) if isinstance(jid, str) else None
    return bool(job and job.get("command")) and speaker_of(job) == ("session", data.get("sender"))


def _by(data):
    return data.get("sender") if data.get("channel") == "session" else WHO["core"] if data.get("channel") == "core" \
        else WHO["owner"]


def apply(st, ev, data, ctx):
    """schedule.set / applied / remove_requested / retarget / due / ended. ctx: new_message, hub."""
    if ev["type"] in FACTS:   # schedule.milestone_removed: the removal itself was folded with its notice or run (_drop)
        return
    jobs = st.setdefault("schedule", {})
    for jid in [j for j, x in jobs.items() if x["state"] in ENDED and x["since"] < ev["ts"] - KEEP_DONE]:
        del jobs[jid]
    kind = ev["type"]
    if kind == "schedule.applied":
        return _applied(st, ev, data, ctx)
    if kind == "schedule.set":
        return _set(st, ev, {**data, "id": ev.get("entity")}, data, ctx)
    job = jobs.get(ev.get("entity"))
    if job is None:
        raise Rejected(T("core.schedule.no_job", "no such job"))
    if kind == "schedule.due":
        slot = data.get("slot")
        if not isinstance(slot, (int, float)) or slot <= (job.get("last_slot") or 0) or slot > ev["ts"] + 60:
            raise Rejected(T("core.schedule.slot_done", "this slot has already fired or has not come yet"))
    move(st, job, kind, ev, data, ctx)


# the core's words to sessions on a firing (core/coretext.py: the session reads the English, the owner his language)
RUN_UNEXPECTED = T("core.schedule.run_unexpected", "this run was not expected or is already recorded")
ORPHAN_GONE = {"removed": T("core.schedule.gone_removed", "removed"), "closed": T("core.schedule.gone_closed", "closed"),
               "failed": T("core.schedule.gone_failed", "crashed")}
ORPHAN_UNLISTED = T("core.schedule.gone_unlisted", "not in the list")
PAUSED_GONE = T("core.schedule.paused_gone", "session {session} is gone ({gone})")
ORPHAN = T("core.schedule.orphan", "[⏰ schedule {id}] session {session} is gone ({gone}) — decide whom to hand it to or "
           "remove it. The job is {what}:")
ORPHAN_RECURRING = T("core.schedule.orphan_recurring", "recurring ({when}) — paused until `hub schedule retarget {id} "
                     "<session>` or `hub schedule rm {id}`")
ORPHAN_ONCE = T("core.schedule.orphan_once", "one-off ({when})")
RAN_UNKNOWN = T("core.schedule.ran_unknown", "[⏰ schedule {id}] the schedule clock restarted while the job's command was "
                "running (the run of {at}): the outcome is unknown, I do not run it a second time. Its output is "
                "logs/schedule/{id}.log in the hub's state folder; if it is needed, run it by hand: {command}")
LATE = T("core.schedule.late", "(late by {minutes} min: the hub or the clock was not running)")
CHECK_NEWS = T("core.schedule.check_news", "The precheck found news — its output:")
CHECK_NEWS_EMPTY = T("core.schedule.check_news_empty", "The precheck found news (no output).")
MORE_NEWS = T("core.schedule.more_news", "More news ({at}) — its output:")
MORE_NEWS_EMPTY = T("core.schedule.more_news_empty", "More news ({at}) (no output).")
CHECK_TIMEOUT = T("core.schedule.check_timeout", "timeout")
CHECK_CODE = T("core.schedule.check_code", "code {code}")
ITS_OUTPUT = T("core.schedule.its_output", "Its output:")
SYSTEM_CHECK_BROKEN = T("core.schedule.system_check_broken", "The precheck of system job {id} failed ({why}) — its code is "
                        "in the core, a command cannot fix it: pass the output to {dev} (`hub send {dev} \"…\"`). Until "
                        "it is fixed there will be no more reminders — the page shows this in red.")
CHECK_BROKEN = T("core.schedule.check_broken", "The precheck `{check}` failed ({why}; 0 — nothing new, 3 — news) — check "
                 "it yourself and fix it (hub schedule add --id {id} … --check)")


def move(st, job, event, ev, data, ctx):
    entity = {"job": job, "req": data, "now": ev["ts"], "slot": data.get("slot")}
    to, todo, why = decide(SCHEDULE, job["state"], event, checker(st, entity))
    if to is None:
        raise Rejected(why)
    if job["state"] != to:
        job["since"] = ev["ts"]
        if to in ("removed", "expired"):   # the job is gone, and so is what it told the owner (core/owner.job_gone)
            from core import owner
            owner.job_gone(st, job["id"])
    job["state"] = to
    tz = st["owner"]["tz"]
    for act in todo:
        if act in ("fire", "skip", "orphan"):
            job["last_slot"] = data["slot"]
        if act == "fire":
            job["last_run"] = ev["ts"]
            if job.get("check") and "exit" in data:
                job["last_check"] = data["exit"]
                _streak(job, data["exit"], ev["ts"])
            if job.get("command"):
                job["runs"] = job.get("runs", 0) + 1
                job["run_slot"] = data["slot"]  # the clock (modules/scheduler) runs it, reports schedule.ran
            elif job.get("check") and data.get("exit") == 0:
                job["quiet"] = job.get("quiet", 0) + 1   # precheck: nothing new — do not wake the session
            elif job.get("system") and _broken(data.get("exit")) and job.get("broken_told") == job["check"]:
                # a system job's check (core/system.py) is the core's code: the session cannot fix it, so it hears of
                # this broken check once, not on every tick (the page shows it, core/system.stops_view)
                job["quiet"] = job.get("quiet", 0) + 1
            elif (queued := _queued(st, job)) is None:
                job["runs"] = job.get("runs", 0) + 1
                _wake(st, job, ev, data["slot"], ctx, _check_note(job, data, st))
                _told(job, data)
            elif job.get("system") and _broken(data.get("exit")):
                coretext.append(queued, _check_note(job, data, st))   # the earlier run still waits: it carries the news
                _told(job, data)
            elif job.get("check") and data.get("exit") == CHECK_NEW:
                # the previous one is still queued, and the check found news (and marked it seen) — append it
                # there, do not lose it
                coretext.append(queued, _check_note(job, data, st, more_at=_local(ev["ts"], tz).strftime("%H:%M")))
        elif act == "skip":
            job["skipped"] = job.get("skipped", 0) + 1
        elif act == "orphan":
            sess = st["sessions"].get(job["session"])
            gone = ORPHAN_GONE.get((sess or {}).get("state"), ORPHAN_UNLISTED)
            # the state keeps the English and its marks (paused_why_l10n): the page and `hub schedule list` show it in
            # the reader's language (state_text, views.schedule_view)
            coretext.put(job, "paused_why", PAUSED_GONE.format(session=job["session"], gone=gone))
            # a core text to a session goes into the journal: the language is explicit, not the process language (the
            # reducer is the same everywhere; the owner sees the phrases in his language from the marks)
            when = when_text(job, tz, "phrase")
            what = (ORPHAN_RECURRING.format(when=when, id=job["id"]) if job["kind"] != "at" else
                    ORPHAN_ONCE.format(when=when))
            hub = ctx["hub"](st)
            if hub:
                ctx["new_message"](st, f"m{ev['seq']}", hub, ORPHAN.format(id=job["id"], session=job["session"], gone=gone,
                                                                           what=what) + " " + job["prompt"],
                                   ev["ts"], origin=forms.ORIGIN_SCHEDULE, schedule=job["id"])
        elif act == "started":
            if not job.get("command") or not job.get("run_slot") or data.get("slot") != job["run_slot"] \
                    or data["slot"] in (job.get("ran_slot"), job.get("started_slot")):
                raise Rejected(RUN_UNEXPECTED)
            job["started_slot"] = data["slot"]
        elif act == "ran":
            if not job.get("command") or data.get("slot") != job.get("run_slot") or job.get("ran_slot") == data["slot"]:
                raise Rejected(RUN_UNEXPECTED)
            unknown = data.get("exit") == "unknown"
            if unknown and job.get("restart_slot") == data["slot"]:   # the command restarted the hub and the clock with it
                data = {"slot": data["slot"], "exit": 0}
                unknown = False
            job["ran_slot"], job["last_exit"], job["last_took"] = data["slot"], data.get("exit"), data.get("took")
            milestone_ran(st, job, data, ev, ctx)   # a milestone that told the owner nothing in this run
            if unknown:   # no code: "the last run failed" on the page would be false
                job.pop("last_exit")
                job.pop("last_took")
            hub = ctx["hub"](st) if unknown and job["state"] != "removed" else None
            if hub:
                # a core text to a session goes into the journal: the language is explicit, not the process
                # language (as in orphan)
                at = _local(data["slot"], tz).strftime("%d.%m %H:%M")
                ctx["new_message"](st, f"m{ev['seq']}", hub, RAN_UNKNOWN.format(id=job["id"], at=at, command=job["command"]),
                                   ev["ts"], origin=forms.ORIGIN_SCHEDULE, schedule=job["id"])
        elif act == "ended" and _told_term(job):
            _term_note(st, job, ev, ctx, ended=True)
        elif act == "retarget":
            target = str(data.get("session") or "").strip()
            if not target:
                raise Rejected(T("core.schedule.retarget_to", "whom to hand it to: session"))
            job["session"] = target
            coretext.put(job, "paused_why", None)


# --- the term of a recurring job: whoever set it hears of it (owner decision 2026-10-30) ---------------------------
# Watchers ended silently and stood for days (a meetings watcher came back after 60 h, two Jira watchers stood 53 h).
# Two days before the term a hubd timer (timer.fired sched:<id>, reducer.pending_timers) and at the end schedule.ended
# give whoever set the job one core note each; the mark is the term itself (warned), so a renewal with a new term
# starts over. A system job is renewed by the core (core/system.due_jobs) and tells nobody.
WARN = 2 * 86400
TIMER = "sched:"
# the setter gets the note itself while it works or is parked, as with the core's other own messages; one put to sleep
# by a human, held in a terminal, handed in or gone — the hub does
UP = ("starting", "idle", "busy", "dialog", "parked")
GONE_WORDS = {"asleep": T("core.schedule.setter_asleep", "asleep"),
              "held": T("core.schedule.setter_held", "in the owner's terminal"),
              "retired": T("core.schedule.setter_retired", "handed in its task and asleep"),
              "removed": T("core.schedule.setter_removed", "removed"), "closed": T("core.schedule.setter_closed", "closed"),
              "failed": T("core.schedule.setter_failed", "crashed")}
SETTER_UNLISTED = T("core.schedule.setter_unlisted", "not in the list")
SET_BY = {WHO["owner"]: T("core.schedule.set_by_owner", "set by the owner"),
          WHO["core"]: T("core.schedule.set_by_core", "set by the core")}
SET_BY_SESSION = T("core.schedule.set_by_session", "set by session {name} — {state}")
WHOM_COMMAND = T("core.schedule.whom_command", "a command")
WHOM_YOU = T("core.schedule.whom_you", "to you")
WHOM_SESSION = T("core.schedule.whom_session", "to session {name}")
GIST = 160
EXTEND_DAYS = DEFAULT_DAYS
# the note is a prompt for the session (the eval case `schedule-term`): the advice is only what to do — renew or remove
TERM_NOTE = T("core.schedule.term_note", "[⏰ schedule {id}] Job {id} ({whom}{who}: \"{gist}\") {when}. Still needed — "
              "extend it: `hub schedule extend {id}` ({days} more days; a term of your own — `--until YYYY-MM-DD`, no "
              "further than {max} days). Not needed — remove it: `hub schedule rm {id}`.")
TERM_SOON = T("core.schedule.term_soon", "ends {at} — it will not fire after that")
TERM_ENDED = T("core.schedule.term_ended", "ended {at} and no longer fires")


def _told_term(job):
    """A recurring job with a term whose setter is to hear of it: not a one-off, not a system one."""
    return job.get("kind") != "at" and job.get("until") is not None and not job.get("system")


def pending(st):
    """(deadline, timer id, term) for hubd: two days before the term of active jobs not yet warned of this term."""
    return [(job["until"] - WARN, TIMER + jid, job["until"]) for jid, job in (st.get("schedule") or {}).items()
            if job["state"] == "active" and _told_term(job) and job.get("warned") != job["until"]]


def warn(st, ev, ctx):
    """The warning timer came: the setter hears "the job ends <when>" once per term. A stale timer (another term, the
    job gone or no longer active) is a fact with no consequences; one that came after the term only marks it (the end
    note will come with schedule.ended) — otherwise hubd would fire it on every tick."""
    job = (st.get("schedule") or {}).get(str(ev.get("entity"))[len(TIMER):])
    term = (ev.get("data") or {}).get("attempt")
    if not job or job["state"] != "active" or not _told_term(job) or term != job["until"] or job.get("warned") == term \
            or ev["ts"] < term - WARN:
        return
    job["warned"] = term
    if ev["ts"] < term:
        _term_note(st, job, ev, ctx, ended=False)


def says_no_data(text):
    """Whether a notice reads "no data" as a whole: the phrase with at most NO_DATA_LABEL other words (numbers count) around
    it. The phrase inside a longer line is the work going on, that job stays: "120 of 500 done, no data for 3 sources, 20
    min left" has nine other words. A line this short that still is real news ("done 5, no data B") is removed after four
    in a row — the cost of a rule that does not read the notice."""
    return bool(NO_DATA.search(text)) and len(re.findall(r"\w+", NO_DATA.sub(" ", text))) <= NO_DATA_LABEL


def is_milestone(job):
    """A progress milestone: a command job named progress-<name> (hosts/streamjson.PROGRESS)."""
    return bool(job.get("command")) and str(job.get("id") or "").startswith(PROGRESS_PREFIX)


def milestone(st, ev, data, ctx):
    """A notice from a milestone's command (owner.notify with schedule_job, bin/hub speaker) was accepted: the job whose
    notices stand still is removed by the core, and whoever set it hears one line (not the owner). A check of a week of
    them (06.11): three of 38 stood after the work was done, 43% of the notices were repeats, one job wrote "no data"
    27 times — the rule "remove it at the end" was forgotten, so the core does it. The same number of "no data" in a
    row: removed. Same text MILESTONE_LIMIT times in a row: removed once nothing works for it (working) — a check of
    the next week (12.11): six of 18 removals hit a long step with a flat number, and every setter set it again the same
    minute. A text that changes every time (minutes of a wait) is not caught."""
    jid = data.get("schedule_job")
    job = (st.get("schedule") or {}).get(jid) if isinstance(jid, str) else None
    if not job or not is_milestone(job) or job["state"] not in ("active", "paused") or \
            not (data.get("channel") == "cli" or from_own_job(st, data)):
        return
    text = " ".join(str(data.get("text") or "").split())
    sig = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]   # the whole text: a change at its end is a change
    job["note_same"] = job.get("note_same", 0) + 1 if job.get("note_sig") == sig else 1
    job["note_sig"] = sig
    job["note_empty"] = job.get("note_empty", 0) + 1 if says_no_data(text) else 0
    job["note_slot"], job["note_none"] = job.get("run_slot"), 0   # this run told the owner something (milestone_ran)
    key, n = ("empty", job["note_empty"]) if job["note_empty"] >= MILESTONE_LIMIT else \
        ("same", job["note_same"]) if job["note_same"] >= MILESTONE_LIMIT and not working(st, job) else (None, 0)
    if key is not None:   # "no data" says more than "no change"
        _drop(st, job, ev, ctx, key, n)


def milestone_ran(st, job, data, ev, ctx):
    """A milestone's run ended (schedule.ran) without a notice to the owner: its source printed nothing, `hub notify`
    refused an empty text or the command failed before it — the owner sees silence, as with no milestone at all. A
    check of a week (12.11): one job ran six times in three hours with no notice, two more had a silent first run, and
    nobody heard of it. Whoever set it hears it at the first such run of a row (once a row: the next notice starts a
    new one); MILESTONE_LIMIT in a row remove it, as repeats do (the removal clears the owner's last milestone too).
    Runs that end well (code 0) are held while its work goes on — a command that notifies on a change only; a failing
    command is a broken source, as "no data", and is not held. A run whose outcome is unknown is not counted."""
    if not is_milestone(job) or job["state"] not in ("active", "paused") or data.get("exit") == "unknown" \
            or job.get("note_slot") == data.get("slot"):
        return
    job["note_none"] = job.get("note_none", 0) + 1
    if job["note_none"] >= MILESTONE_LIMIT and (data.get("exit") != 0 or not working(st, job)):
        _drop(st, job, ev, ctx, "silent", job["note_none"])
    elif job["note_none"] == 1:
        _tell(st, job, ev, ctx, "milestone_quiet", code=data.get("exit"))


def working(st, job):
    """Whether the work a milestone tells of goes on, by the core's facts tied to it, not by its text: a task or a
    `hub run` started in a turn that set the milestone (one of its chain roots, job.roots) by its setter, or by the
    session it was handed to, is still going — the task held by an executor that is up. The roots outlive a removal:
    a set again right after it (in the turn the core's note started, which has no root of its own) keeps the tasks. Not "the setter is in a
    turn" or "any executor of it works": a check of 17 removals (12.11) — a lead with many tasks always has some, and
    that kept seven of nine milestones of finished work; the chain root keeps none. Nor a background command: nothing
    ties one to the milestone (a Monitor or a dev server would hold a finished one)."""
    leads = {job.get("by") or "", job.get("session") or ""} - {""}
    roots = set(job.get("roots") or [job.get("root")]) - {None}
    if not roots:
        return False
    if any(run.get("root") in roots for name in leads for run in hubrun.live_of(st, name)):
        return True
    return any(d.get("by") in leads and d.get("root") in roots and d.get("state") in TASK_OPEN
               and (st["sessions"].get(d.get("session") or "") or {}).get("state") in UP
               for d in (st.get("delegations") or {}).values())


def _drop(st, job, ev, ctx, why, n=0, tell=True):
    """The core removes a milestone: the move (folded with the notice or the run that decided it), a fact in the
    journal with the reason (schedule.milestone_removed: hubd writes it, core/daemon.py; it records, it changes no
    state — an older core rejects the line with no consequences), and a line to whoever set it."""
    move(st, job, "schedule.remove_requested", ev, {"channel": "core"}, ctx)
    st.setdefault("_facts", []).append({"do": "milestone_removed", "job": job["id"], "why": why, "n": n,
                                        "by": job.get("by"), "of": ev["seq"]})
    if tell:
        _tell(st, job, ev, ctx, f"milestone_{why}", n=n)


def _tell(st, job, ev, ctx, key, **values):
    from core import i18n
    to, who = _setter(st, job, ctx)
    if to:
        # a core text to a session goes into the journal: the language is the hub's (the reducer is the same everywhere)
        ctx["new_message"](st, f"m{ev['seq']}", to, i18n.tr(i18n.state_lang(st), f"core.schedule.{key}", id=job["id"],
                                                              **values) + who, ev["ts"], origin=forms.ORIGIN_CORE)


def drop_milestones(st, name, ev, ctx):
    """Session name was removed: the milestones it set (or was handed) tell about work nobody leads now — removed."""
    for job in list((st.get("schedule") or {}).values()):
        if is_milestone(job) and job["state"] in ("active", "paused") and (job.get("session") or job.get("by")) == name:
            _drop(st, job, ev, ctx, "gone", tell=False)


def _setter(st, job, ctx):
    """(whom to tell, who set it — for the hub): the session that set the job while it works or is parked; otherwise the
    hub with a word on who set it. No hub — (None, "")."""
    by = job.get("by") or ""
    hub = ctx["hub"](st)
    sess = st["sessions"].get(by)
    if sess and sess["state"] in UP:
        return by, ""
    if by == hub:
        return hub, ""
    who = SET_BY.get(by) or SET_BY_SESSION.format(name=by, state=GONE_WORDS.get((sess or {}).get("state"),
                                                                                  SETTER_UNLISTED))
    return hub, "; " + who


def term_text(jid, whom, who, gist, at, ended):
    """The core's note on a job's term (a separate function: the eval builds the same text)."""
    return TERM_NOTE.format(id=jid, whom=whom, who=who, gist=gist, days=EXTEND_DAYS, max=MAX_DAYS,
                            when=(TERM_ENDED if ended else TERM_SOON).format(at=at))


def _term_note(st, job, ev, ctx, ended):
    to, who = _setter(st, job, ctx)
    if not to:
        return
    whom = WHOM_COMMAND if job.get("command") else WHOM_YOU if job.get("session") == to else \
        WHOM_SESSION.format(name=job["session"])
    text = " ".join((job.get("command") or job.get("prompt") or "").split())
    gist = text[:GIST] + ("…" if len(text) > GIST else "")
    at = _local(job["until"], st["owner"]["tz"]).strftime("%d.%m %H:%M")
    # a core text to a session goes into the journal: the language is explicit (as in orphan)
    ctx["new_message"](st, f"m{ev['seq']}", to, term_text(job["id"], whom, who, gist, at, ended), ev["ts"],
                       origin=forms.ORIGIN_CORE)


def extend_spec(job, until, now, tz):
    """`hub schedule extend <id> [--until YYYY-MM-DD]`: the job as it is with a new term — by default 14 more days from
    its term (from today if it has passed), no further than the 30-day limit; the core checks it as any schedule.set.
    Only an active or expired recurring job with a term. Invalid — what is wrong."""
    from core import i18n   # only `hub schedule extend` asks (bin/hub prints the reason): the process language
    if job is None:
        raise Invalid(i18n.t("core.schedule.extend_no_job"))
    if job.get("kind") == "at" or job.get("system") or job.get("until") is None:
        raise Invalid(i18n.t("core.schedule.extend_what"))
    if job["state"] not in ("active", "expired"):
        raise Invalid(i18n.t("core.schedule.extend_paused", id=job["id"]) if job["state"] == "paused" else
                      i18n.t("core.schedule.extend_ended"))
    if not until:
        # the last full day before the limit: "D+30 23:59" may pass now + 31 days over a clock change
        day = lambda ts: _local(ts, tz).date()  # noqa: E731
        end = min(day(max(job["until"], now)) + datetime.timedelta(days=EXTEND_DAYS),
                  day(now) + datetime.timedelta(days=MAX_DAYS - 1))
        if job["state"] == "active" and end <= day(job["until"]):
            raise Invalid(i18n.t("core.schedule.extend_longest", date=day(job["until"]).strftime("%d.%m")))
        until = end.isoformat()
    spec = {"id": job["id"], job["kind"]: job[job["kind"]], "until": until}
    for k in ("prompt", "command", "check", "check_timeout", "wait"):
        if job.get(k):
            spec[k] = job[k]
    if not job.get("command"):
        spec["session"] = job["session"]
    return spec


def state_text(job, now, tz, lang=None):
    """The job's state in `hub schedule list` words: the next run, why it is paused, when it ended (and what to do)."""
    from core import i18n
    tr = lambda key, **kw: i18n.tr(lang or i18n.current(), key, **kw)  # noqa: E731
    fmt = lambda ts: _local(ts, tz).strftime("%d.%m %H:%M")  # noqa: E731
    if job["state"] == "active":
        nd = next_due(job, now, tz)
        return tr("core.schedule.state_next", at=fmt(nd)) if nd else tr("core.schedule.state_never")
    if job["state"] == "paused":
        return tr("core.schedule.state_paused", why=coretext.human(coretext.got(job, "paused_why"),
                                                                  lang or i18n.current()) or "")
    if job["state"] == "expired" and job.get("until") is not None:
        return tr("core.schedule.state_ended", at=fmt(job["until"]), id=job["id"])
    return job["state"]


def _broken(code):
    """A precheck code that is an error (another code than 0 and 3, a timeout); None — there was no check."""
    return code is not None and code not in (0, CHECK_NEW)


def _told(job, data):
    """The session heard that this system check is broken (which check: a changed one is told again)."""
    if job.get("system") and _broken(data.get("exit")):
        job["broken_told"] = job["check"]


def _streak(job, code, ts):
    """Precheck ticks with news in a row (news, since news_since) and when such a run ended with a quiet tick
    (news_cleared): the page shows whether a check's session acts on it (core/system.stops_view). An error code is not
    news and does not end the run."""
    if code == CHECK_NEW:
        if not job.get("news"):
            job["news_since"] = ts
        job["news"] = job.get("news", 0) + 1
    if not _broken(code):
        job.pop("broken_told", None)
    if code == 0:
        if job.get("news"):
            job["news_cleared"] = ts
        job["news"] = 0
        job.pop("news_since", None)


def _queued(st, job):
    """The previous run of this job still waits in the queue (the session waits for a form answer, or is busy) — we do
    not queue a copy (04.10: one job queued three times in four hours to a busy session); for `hub wait` — any check of
    the same wait (09.10: 14 checks of two waits queued for a session busy all night). Returns that message or None."""
    m = next((m for m in st["messages"].values()
              if m["state"] == "queued" and m.get("schedule") and kin(m["schedule"]) == kin(job["id"])), None)
    if m is not None:
        job["skipped"] = job.get("skipped", 0) + 1
    return m


def _wake(st, job, ev, slot, ctx, extra=""):
    late = int((ev["ts"] - slot) // 60)
    note = " " + LATE.format(minutes=late) if late >= 5 else ""
    ctx["new_message"](st, f"m{ev['seq']}", job["session"],
                       coretext.join(f"[⏰ schedule {job['id']}]", note, " " + job["prompt"], extra),
                       ev["ts"], origin=forms.ORIGIN_SCHEDULE, schedule=job["id"])


def _check_note(job, data, st=None, more_at=None):
    """What to tell the session about the precheck outcome: 3 — its output; another code or a timeout — a check error.
    Without a check or without a code in the slot (a clock of an old version) — nothing, the session gets the job
    as is. more_at — the time of a later run whose news goes onto the queued message ("More news (HH:MM)")."""
    if not job.get("check") or "exit" not in data:
        return ""
    code, out = data["exit"], str(data.get("out") or "").strip()[:CHECK_OUT + 500]
    if code == CHECK_NEW:
        if more_at is not None:
            return "\n\n" + (MORE_NEWS.format(at=more_at) + "\n" + out if out else MORE_NEWS_EMPTY.format(at=more_at))
        return "\n\n" + (CHECK_NEWS + "\n" + out if out else CHECK_NEWS_EMPTY)
    why = CHECK_TIMEOUT if code == "timeout" else CHECK_CODE.format(code=code)
    output = ITS_OUTPUT + "\n" + out if out else ""
    if job.get("system"):   # its check is in the core: `hub schedule add` refuses a system job
        dev = ((st or {}).get("system") or ["hub-dev"])[0]   # Hub Developer, the first system agent (core/system.specs)
        return "\n\n" + SYSTEM_CHECK_BROKEN.format(id=job["id"], why=why, dev=dev) + (" " + output if out else "")
    return "\n\n" + CHECK_BROKEN.format(check=job["check"], why=why, id=job["id"]) + (". " + output if out else ".")


def _set(st, ev, spec, data, ctx):
    jobs = st["schedule"]
    try:
        new = normalize(spec, ev["ts"], st["owner"]["tz"])
    except Invalid as e:
        raise Rejected(str(e)) from None
    old = jobs.get(new["id"])
    if new["command"] and new["session"]:
        # whom to hand the command to (like retarget); a live session — otherwise "accepted" with no effect (24.10:
        # a typo in the name)
        if (st["sessions"].get(new["session"]) or {}).get("state") in (None, "removed"):
            raise Rejected(T("core.schedule.no_command_session", "session: there is no session {name} — nobody to hand "
                             "the command to").format(name=new["session"]))
        if old is None and data.get("channel") in ("session", "plugin") and data.get("sender") != new["session"] \
                and not _sender_is_hub(st, data):
            # a new command cannot be slipped to another session: the hub, the owner or the session itself may hand
            # it over (an existing one — may_edit_job)
            raise Rejected(T("core.schedule.command_for_other", "a new command for another session is handed over by the "
                             "hub or the owner; set it for yourself and hand it over: hub schedule retarget"))
    elif new["command"] and old and old.get("command"):
        new["session"] = old.get("session") or ""   # whom it was handed to (retarget) — the same set does not take it back
    same_timing = old is not None and all(new.get(k) == old.get(k) for k in TIMING)
    if same_timing and not spec.get("until") and old["state"] == "expired":
        # the same set without a term does not revive an ended job with its passed term (it would end again at once and
        # tell its setter again): `hub schedule apply` leaves it as it is (edits too), a direct set says how to renew it;
        # who may not edit the job hears that first
        why = may_edit_job(st, {"job": old, "req": data})
        if why:
            raise Rejected(why)
        if ev["type"] == "schedule.set":
            raise Rejected(T("core.schedule.term_over", "the job's term is over — to extend it: hub schedule extend {id} "
                             "(or --until YYYY-MM-DD)").format(id=old["id"]))
        return
    if same_timing:
        if not spec.get("until"):
            new["until"] = old["until"]  # the same set again — the deadline does not extend itself
        if new["kind"] == "every":
            new["start"] = old["start"]
        if old["state"] in ("active", "paused") and \
                all(new.get(k) == old.get(k) for k in ("session", "prompt", "command", "check", "check_timeout", "until", "wait")):
            return  # nothing changed: a repeated apply is not an event
    before = (old or {}).get("until")
    # a milestone is new when there was none, it was removed, or its command changed: its first notice is at the start
    fresh = new["command"] and is_milestone(new) and (old is None or old["state"] in ("removed", "done")
                                                      or old.get("command") != new["command"])
    # who wrote the text speaks with it: the owner's command a session rewrote is the session's; a new timing or term of
    # the same text (`hub schedule extend`, the page) is not writing it — the owner extending a session's job does not
    # hand its command his rights, nor the hub demote the owner's; nor does renewing an expired one (read before the job
    # takes the new fields)
    wrote = old is None or old.get("command") != new["command"]
    job = old or {"id": new["id"], "state": SCHEDULE["initial"], "by": _by(data), "created": ev["ts"],
                  "since": ev["ts"], "runs": 0}
    move(st, job, "schedule.set", ev, data, ctx)
    jobs[job["id"]] = job
    if not same_timing:
        job.pop("last_slot", None)
        job["created"] = ev["ts"]
    for k in ("at", "every", "start", "cron", "check", "check_timeout", "wait"):
        job.pop(k, None)
    job.update(new)
    if not new["command"]:
        job.pop("command_by", None)
    elif wrote:
        job["command_by"] = _voice(data)
    coretext.put(job, "paused_why", None)
    if fresh:
        for k in MILESTONE_KEYS:
            job.pop(k, None)
        job["run_slot"], job["last_run"], job["runs"] = ev["ts"], ev["ts"], job.get("runs", 0) + 1   # the clock runs it at once
    if data.get("channel") == "core" and data.get("system"):
        job["system"] = True   # core/system.py: only the core can remove or change it
    if _told_term(job) and job["until"] != before and job["until"] - ev["ts"] <= WARN:
        job["warned"] = job["until"]   # set for two days or less: the setter has just chosen the term, no "ends soon"
    # in whose chain it was set (core/chain.py): the firing result goes there too; outside a session turn — no chain
    root = chain.turn_root(st, data.get("sender")) if data.get("channel") == "session" else None
    if root:
        if is_milestone(job):   # the turns that set it: their tasks are its work (working), a later set adds its own
            had = [r for r in job.get("roots") or [job.get("root")] if r and r != root]   # a job of an older core: its root
            job["roots"] = had[-(MILESTONE_ROOTS - 1):] + [root]
        job["root"] = root
    if data.get("channel") == "session" and chain.turn_scheduled(st, data.get("sender")):
        job["via_schedule"] = True   # set during a scheduled turn: its firings are not live words either (core/budget)
    else:
        job.pop("via_schedule", None)


def _applied(st, ev, data, ctx):
    """The wanted set of jobs as a whole (the hub tidies up declaratively): what is not in the set is removed, the rest
    is set. The scope is the sender's jobs; the hub or the owner may take the jobs of one session: scope.session."""
    wanted = data.get("jobs")
    if not isinstance(wanted, list):
        raise Rejected(T("core.schedule.jobs_list", "jobs: a list of jobs"))
    ids = [str((j or {}).get("id") or "") for j in wanted]
    if len(set(ids)) != len(ids):
        raise Rejected(T("core.schedule.ids_repeat", "ids repeat in the set"))
    scope = data.get("scope") or {}
    by = _by(data)
    for job in list(st["schedule"].values()):
        mine = owner_of(st, job) == scope["session"] if scope.get("session") else job.get("by") == by
        if mine and not job.get("system") and job["state"] in ("active", "paused", "expired") and job["id"] not in ids:
            move(st, job, "schedule.remove_requested", ev, data, ctx)
    for spec in wanted:
        _set(st, ev, {**spec, "id": str(spec.get("id"))}, data, ctx)
