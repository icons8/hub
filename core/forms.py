"""The core's own phrases that the core also reads back (language spec, slice S0): who wrote a message, what a `hub
wait` check is waiting for, a core failure, a gateway that did not answer, the weekly limit, "Waiting for the owner's
yes", "### For sessions", the owner's language.

Field first: the event or record field (from, wait, code, why_code, yes_note, head) is written by whoever writes the
text. The text is parsed only when there is no field (old journal, snapshot, archive, module edit) or the value is
unknown. Every text form is both the current one and the future English one: later slices translate the texts, and a
rollback to this slice understands them too.
New forms come only from this list (tests/test_forms.py is the contract). The Russian forms are language data
(core/langdata.py, `legacy` and `words`): the code names them, the data holds their text.
"""
import re
import unicodedata

from core import langdata
from core import coretext
from core.coretext import T

# --- who wrote the message: the from field = {kind, name?} next to the text label origin ---------------------------

FROM_KINDS = ("owner", "session", "hub", "core", "schedule", "module", "plugin", "member")
NAMED = ("session", "module", "plugin")   # these also carry a name

_ORIGIN = langdata.legacy("origin")   # the labels of the Russian texts: earlier journals, transcripts and snapshots
OLD_FROM = langdata.legacy("from")   # the label's preposition of the Russian texts
FROM = "from"   # the preposition the core writes (slice C4): [hub:mN from user], [hub:mN from maps, ...]
ORIGIN_CORE, ORIGIN_HUB, ORIGIN_OWNER, ORIGIN_SCHEDULE = " from core", " from hub", " from user", " from schedule"
ORIGIN_PLUGIN = " from plugin"
# a member of the hub's Telegram group who is not its admin: asks, is not the owner (modules/telegram, spec d2513429)
ORIGIN_MEMBER = " from group member"
OLD_ORIGINS = {_ORIGIN[k]: k for k in ("core", "hub", "owner", "schedule")}
# a label without tails says who it is from on its own (new_message sets from itself); other writers name from explicitly
FIXED = {ORIGIN_CORE: "core", ORIGIN_HUB: "hub", ORIGIN_OWNER: "owner", ORIGIN_SCHEDULE: "schedule", ORIGIN_MEMBER: "member",
         **OLD_ORIGINS}

# the client that sent the owner's message goes into the label after the owner word: ORIGIN_OWNER + " · Android 0.2.7"
# (POST /api/send {app, app_version}); the tail is not a part of the signature: a reader takes the word before it
APPS = {"web": "Web", "ios": "iOS", "mac": "Mac", "android": "Android", "telegram": "Telegram"}
APP_SEP = " · "
# a build too ("2.14 (412)"); no "," "]" "[" or control characters: they break the label. ASCII only: Unicode \w lets in
# letters the eye does not see (U+3164) — into the label the session reads as the owner's signature
APP_VERSION = re.compile(r"[\w.+() -]{1,20}", re.ASCII)


def app_tail(app, version=None):
    """The label tail for a client: " · Android 0.2.7"; an unknown client or a bad version - nothing (the label as before)."""
    if not (isinstance(app, str) and app in APPS):
        return ""
    ok = isinstance(version, str) and APP_VERSION.fullmatch(version) and version.strip() == version and version
    return APP_SEP + APPS[app] + (" " + version if ok else "")


def owner_origin(data):
    """The label of the owner's own message: ORIGIN_OWNER plus the client when the message names one."""
    return ORIGIN_OWNER + app_tail(data.get("app"), data.get("app_version"))


def fixed_kind(origin):
    """The kind of a label with no names in it (core, hub, owner, schedule), also with a client tail; else None."""
    return FIXED.get((origin or "").split(APP_SEP, 1)[0])


# the English labels: " from core", " from hub", " from user", " from schedule", " from module X", " from plugin X",
# " from <session>"; the Russian ones of earlier texts are read too
CORE_ORIGINS = (ORIGIN_CORE, _ORIGIN["core"])
HUB_ORIGINS = (ORIGIN_HUB, _ORIGIN["hub"])
OWNER_ORIGINS = (ORIGIN_OWNER, _ORIGIN["owner"])
PLUGIN_ORIGINS = (ORIGIN_PLUGIN, _ORIGIN["plugin"])
# the word after the label's preposition -> kind; words are of their own language only: Russian "from user" is a session
# named user, English " from user" is the owner
WORDS = {OLD_FROM: dict(langdata.legacy("from_words")),
         FROM: {"core": "core", "hub": "hub", "user": "owner", "schedule": "schedule", "group member": "member"}}
NAMED_WORDS = {OLD_FROM: tuple(tuple(x) for x in langdata.legacy("from_named")),
               FROM: (("module ", "module"), ("plugin ", "plugin"), ("session ", "session"))}
NAMED_LABEL = {kind: head + "{name}" for head, kind in NAMED_WORDS[FROM]}   # "module X", "plugin X", "session X"


def from_session(name, hub=False):
    """The label of a session's message: " from <name>". A name that is one of the words of the label (user, core,
    hub, schedule) would read as that sender — the owner's word for a session named user — so it goes as
    " from session <name>" (who_kind reads it back; a core before it reads a session "session <name>", not the owner).
    Only the hub session itself (hub) keeps its name as is: "from hub" from the hub session is the hub."""
    name = str(name)
    taken = name in WORDS[FROM] and not (hub and name == "hub")
    return f" {FROM} {NAMED_LABEL['session'].format(name=name) if taken else name}"


def is_hub(st, name):
    """Does a message from name come from the hub: the hub session of state st (its kind, not its name), or the word hub
    with no ordinary session of that name (the address hub is the hub, identity.HUB_WORDS)."""
    sess = (st or {}).get("sessions", {}).get(name)
    return (sess or {}).get("kind") == "hub" or (name == "hub" and sess is None)


def from_named(kind, name):
    """The label of a module's or a plugin's message: " from module X" / " from plugin X"."""
    return f" {FROM} {NAMED_LABEL[kind].format(name=name)}"


def outside_id(sender):
    """A sender that is a module or a plugin process: mod:<name> / ext:<name> - the way the core writes them
    (core/spool.outsider); in older plugin facts the name has no prefix, which means ext:<name>."""
    sender = str(sender)
    return sender if sender.startswith(("mod:", "ext:")) else f"ext:{sender}"


def origin_from(kind, name=None):
    """The from field: {kind} or {kind, name}."""
    return {"kind": kind, "name": str(name)} if kind in NAMED else {"kind": kind}


def sender(msg):
    """The message's from field if it is present and understood, else None - then read the label text (msg.origin) the old way."""
    f = (msg or {}).get("from")
    if not isinstance(f, dict) or f.get("kind") not in FROM_KINDS:
        return None
    if f["kind"] in NAMED and not isinstance(f.get("name"), str):
        return None
    return f


def is_core(msg):
    """A core service message: by the field; without it, the label is exactly "from core" (as it was compared before)."""
    f = sender(msg)
    if f:
        return f["kind"] == "core"
    return (msg or {}).get("origin") in CORE_ORIGINS


def who_kind(prep, who):
    """The label's signature after the preposition prep (Russian or English "from") -> (kind, name): a word of its own language is a
    kind without a name, "module X" (ru or en) is module X, anything else is a session with that name. A client tail
    (" · Android 0.2.7") is not a part of the signature."""
    who = who.split(APP_SEP, 1)[0]
    if who in WORDS[prep]:
        return WORDS[prep][who], None
    for head, kind in NAMED_WORDS[prep]:
        if who.startswith(head):
            return kind, who[len(head):]
    return "session", who


def parse_origin(origin):
    """A message's text label ("<ru from> X, ..." / future "from X, ...") -> (kind, name); no label with a preposition - (None, None)."""
    o = (origin or "").strip()
    for prep in WORDS:
        if o.startswith(prep + " "):
            return who_kind(prep, o[len(prep) + 1:].split(",", 1)[0].strip())
    return None, None


# --- the label in a session turn's text: [hub:mN] / [hub:mN <ru from> X, ...] / [hub:mN from X, ...] (only the hook sees it) --

MARKER_BY = (f" {OLD_FROM} ", " from ")
# message id - mN and any tail the core adds: mNxK, mNaK (MCP entry), mNo, mNd, mNx5-<session> (the hub name comes from
# hub.json, any value). The tail runs up to "]", a space or "," (19.10: an id with a tail was not recognised - the turn had
# no label, the message was not delivered, the "Terminal input" feed showed the raw label). One rule for the hook, the
# "Stream" and the modules (corewrite)
MSG_TAIL = r"[^\]\s,]*"
MARKER = re.compile(rf"\[hub:(m\d+{MSG_TAIL})(?:\]| {OLD_FROM} | from )")
LINE_MARKER = re.compile(rf"(?m)^\[hub:(m\d+{MSG_TAIL})(?:\]| {OLD_FROM} | from )")
HEAD = re.compile(rf"\[hub:(\w{MSG_TAIL})(?: ({OLD_FROM}|from) ([^,\]]*))?[,\]]")   # label preposition and signature up to "," or "]"
LINE_HEAD = re.compile(rf"(?m)^{HEAD.pattern}")   # the same at line start - a label the core put there
SCHEDULE_MARKS = (ORIGIN_SCHEDULE.lstrip() + "]", _ORIGIN["schedule"].lstrip() + "]")
# Claude Code's notice of a background task: the end of a command, an agent or a monitor carries <status> (one block
# may name several tasks: those a previous process left unfinished); a monitor's event (`Monitor event`, one per line of
# its output) carries the same task id and no status — except Claude Code's own last event of a monitor: expired at its
# timeout, stopped (too much output) or timed out (Claude Code 2.1.309 strings)
NOTICE_BLOCK = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
NOTICE_TASK = re.compile(r"<task-id>(\w+)</task-id>")
NOTICE_STATUS = re.compile(r"<status>\w+</status>")
NOTICE_EVENT = re.compile(r"<event>(.*?)</event>", re.S)   # the watched command's own output: its text says nothing
NOTICE_RESULT = re.compile(r"<result>.*?</result>", re.S)  # an agent's own words: neither
NOTICE_NEXT = re.compile(r"\s*<task-notification>(.*?)</task-notification>", re.S)
MONITOR_END = re.compile(r"^\[Monitor (?:expired|stopped|timed out)\b")


def ended_tasks(text):
    """Ids of the background tasks a text says have ended: those of <task-notification> blocks with a status of their
    own or a monitor's last event (11.11: every monitor event was taken for the end — an executor watching its runs fell
    asleep and its runs were killed). Only Claude Code's notices: the blocks one after another from the start of the text
    — a message glued after them may quote one; inside a block, not the command's output or the agent's result. One rule
    for the hook (notice.ended) and the host (a notice mid-turn)."""
    ids, text, at = [], str(text or ""), 0
    while (m := NOTICE_NEXT.match(text, at)):
        at = m.end()
        block = m.group(1)
        events = NOTICE_EVENT.findall(block)
        own = NOTICE_RESULT.sub("", NOTICE_EVENT.sub("", block))   # what Claude Code itself wrote
        if NOTICE_STATUS.search(own) or any(MONITOR_END.match(e) for e in events):
            ids += [t for t in NOTICE_TASK.findall(own) if t not in ids]
    return ids
# before a label at line start - spaces of any kind, control, invisible and combining characters: the eye does not see them
INVISIBLE = {"Zs", "Zl", "Zp", "Cc", "Cf", "Mn", "Me"}


def quote_labels(text):
    """`[hub:` at line start is a core label: in message text and the notes attached to it it becomes a quote "> [hub:".
    Otherwise foreign text passed for a label: the session read it as the owner's word, the hook (LINE_MARKER) as one more
    message of that turn. A line is split by any line break (`\\r`, U+2028...); before the label, anything from
    INVISIBLE may stand."""
    lines = str(text).splitlines(keepends=True)
    for i, line in enumerate(lines):
        lead = 0
        while lead < len(line) and unicodedata.category(line[lead]) in INVISIBLE:
            lead += 1
        if line.startswith("[hub:", lead):
            lines[i] = line[:lead] + "> " + line[lead:]
    return "".join(lines)


# --- headers before the words of a task - the executor name without them (executors.auto_name) --------------------
# Future English ones are only those the core itself writes (a task, an answer to question qN): a name derived from the
# owner's text that starts in English ("Hub, ...", "Context: ...") must stay the same - folding the journal tail with the
# old and the new code gives one name. The Russian "Context", "Hub,", "voice:" are written by people, sessions and the
# page - their forms are translated by their own slices.

VOICE = "🎤 voice: "            # the head of a transcription in English; the bot and the page write the word in the owner's
VOICE_HEAD = r"[^\s:]{1,24}:\s*"   # language (telegram.voice_head, web.voice.head): after the mic, one word of any language
HEADS = (
    r"^\s*\[hub:[^\]]*\]\s*",
    # Russian: "The owner's answer to question qN (the question is stale - ...):", "Answer to question qN ("..."):",
    # "This is the owner's answer to your question qN ("...").", "..., passed on by the hub:" - and the future "The user's
    # answer to question qN ...", "This is the user's answer to your question qN ("..."), passed on by the hub:",
    # "Answer to question qN ("..."):"
    r"^\s*(?:" + langdata.legacy("head_answer") +
    r"|(?:This is )?(?:[Tt]he )?(?:[Uu]ser's answer|Answer) to (?:your )?question) q\d+"
    r"(?: \(«[^»\n]*»\)| \([^)\n]*\))*(?:" + langdata.legacy("head_passed") + r"|, passed on by the hub)?[:.]\s*",
    r"^\s*(?:" + langdata.legacy("head_task") + r"|Task from) [^\n]*?\.\s*(?:(?:" + langdata.legacy("head_words") +
    r"|Their words):\s*)?",
    langdata.legacy("head_context"),
    # a transcription the bot and the page send: "🎤 <the word voice in the owner's language>: …" (the Russian word was
    # also typed without the mic); any word only after the mic — an owner's text starting with it keeps its name
    r"^[\s>]*(?:" + langdata.legacy("head_hub") + r")?(?:🎤\s*(?:" + VOICE_HEAD + r")?)?(?:" + langdata.legacy("head_voice")
    + r")?",
)


def strip_heads(text):
    t = str(text)
    for head in HEADS:
        t = re.sub(head, "", t)
    return t


# --- the `hub wait` check: the job's wait field, without it - the job's text -----------------------------------------

WAIT_MARKS = (f"[{langdata.legacy('wait')}]", "[waiting for reply]")
WAIT_WHAT = re.compile(r"^\[(?:" + langdata.legacy("wait") + r"|waiting for reply)\] (.*?)\. (?:"
                       + langdata.legacy("check_now") + r"|Check now);", re.S)


def wait_gist(prompt):
    """The text of a `hub wait` check job up to its "Check now" tail (the page's row of a wait); a text with the head
    and no such tail - whole; not a check - None."""
    text = str(prompt or "")
    m = WAIT_WHAT.match(text)
    if m:
        return m.group(0)[:m.end(1)]
    return re.split(r"\. (?:" + langdata.legacy("check_now") + r"|Check now)", text, 1)[0] \
        if text.startswith(WAIT_MARKS) else None

# --- reason codes: the code field (core.rejected, gateway) and why_code (a queued request), without it - the text start --

CORE_FAILURE = "core_failure"   # an error in the folding code (reducer.reduce)
NO_ANSWER = "no_answer"         # the account gateway did not answer - the core switched it off itself
WEEKLY_LIMIT = "weekly_limit"   # the account's weekly limit (executors.budget_waits)
POOL_LIMIT = "pool_limit"       # the limit of the whole pool (budget, owner mode)
ECONOMY = "economy"             # limit economy (budget, economy mode)
DUPLICATE = "duplicate"         # a repeated module dedup_key or page client_id (modstate.dedup_check): field only
WAIT_CODES = (WEEKLY_LIMIT, POOL_LIMIT, ECONOMY)   # why a queued request waits
_OLD_PREFIX = langdata.legacy("prefixes")
PREFIXES = {CORE_FAILURE: (_OLD_PREFIX[CORE_FAILURE], "core failure"), NO_ANSWER: (_OLD_PREFIX[NO_ANSWER], "not answering"),
            WEEKLY_LIMIT: (_OLD_PREFIX[WEEKLY_LIMIT], "weekly limit"), POOL_LIMIT: (_OLD_PREFIX[POOL_LIMIT], "pool limit"),
            ECONOMY: (_OLD_PREFIX[ECONOMY], "limit economy")}


class Why(coretext.Text):   # a Text: a phrase with it, or it with a phrase, keeps the marks
    """A reason as text and its code (a JSON string as before; the code is for whoever writes the field next to it).
    The marks of the core's phrases in the text (core/coretext.py) go with it: a refusal is shown to the owner in his
    language."""
    def __new__(cls, text, code=None):
        s = super().__new__(cls, text)
        s.code = code
        s.spans = getattr(text, "spans", ())
        return s


def code_of(why):
    return getattr(why, "code", None)


# waits in the queue, no reasons: only the daemon pass is left (the why of a queued request: hub task queue shows it)
READY_TO_RISE = T("core.forms.ready_to_rise", "there is room — it will come up by itself")


def why_fields(why):
    """Queue request fields: why - as text (a JSON string), why_l10n - the marks of the core's phrases in it (the owner
    reads it in his language: coretext.got), why_code - the code, if there is one. Whoever replaces them pops why_code
    and why_l10n first."""
    code = code_of(why)
    return {**coretext.put({}, "why", why), **({"why_code": code} if code else {})}


def is_reason(code, given, text):
    """A reason with this code: by the field (given is the code next to the text), without it or with an unknown one - by the text start."""
    if isinstance(given, str) and given in PREFIXES:
        return given == code
    return str(text or "").startswith(PREFIXES[code])


def reason_code(given, text, among):
    """The reason code from the list among: the field, without it (or unknown) - by the text start; not found - None."""
    if isinstance(given, str) and given in among:
        return given
    return next((c for c in among if str(text or "").startswith(PREFIXES[c])), None)


# --- "Waiting for the owner's yes" in a task's context: the yes_note field (the exact string), without it - the line start --

YES_NOTE_HEADS = (langdata.legacy("yes_note"), "Waiting for the owner's yes")

# --- the bot's header in Telegram above a session's reply: the head field of the route, without it - the forms -------

REPLY_HEADS = ("{name}:", langdata.legacy("reply_head"), "{name} replies:")

# --- CHANGES.md: items for sessions ---------------------------------------------------------------------------------

SESSION_SECTIONS = (langdata.legacy("session_section"), "### For sessions")

# --- the owner's language: a code; the old words in state are read as codes -------------------------------------------

LANG_CODES = {**langdata.legacy("lang_codes"), "Russian": "ru", "English": "en", "ru": "ru", "en": "en"}
LANG_WORDS = {"ru": "Russian", "en": "English"}   # the word in the rule for sessions (the rules are in English)


def lang_code(value):
    """The language code from the owner.lang/langs field: "ru", "en", the old Russian words and the English names for
    Russian/English; unknown - as is."""
    return LANG_CODES.get(value, value) if isinstance(value, str) else value
