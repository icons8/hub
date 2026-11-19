"""The core's own phrases that it also reads (languages spec, slice C0; core/forms.py): parsing goes by an event or record
field, text only when there is no field (an old journal, snapshot, archive) or the value is unknown. Every text form is
the current one plus the future English one: the next slices translate the texts, and a rollback to this slice
understands them too.

For each parsing place: the field is there; no field (an old form); an unknown value (→ text); field against text (the
field is trusted); a future text form. The contract (the last class): future forms are only these."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import re
import subprocess
import sys
import tempfile
import unittest

from core import (budget, coretext, corewrite, delegations, executors, flow, forms, i18n, lessons, minihub, news,
                  reducer, schedule, spool, views)
from hooks import hub_hook
from core import cutout  # noqa: E402 — the open flavor's run has no built-in gateway (core/cutout.py)
gateway = None
if cutout.present("gateway"):
    from modules.gateway import gateway
from modules.telegram import bot as tg
from modules.watch import watch
from tests.fakes import lang

OWNER = {"channel": "web", "owner": True}
# the earlier Russian forms (language data): the label's preposition, sender identifiers, the hub's signature words
FROM = lang.legacy("from")
OLD_WHO = lang.legacy("who")
OLD_VOICE = OLD_WHO["module"].format(name="voice")   # module voice after the Russian preposition
# the senders the core names now: views._who gives the page codes, views._sender the "Flow" nodes (slice C4)
WHO = {"core": ":core", "schedule": ":schedule", "owner": ":owner"}
MODULE_VOICE, PLUGIN_THEMES = "module voice", "plugin themes"
WHO_VOICE, WHO_THEMES = "mod:voice", "ext:themes"
HUB_OLD = next(iter(lang.legacy("flow_words")))   # the hub's signature of earlier core versions
HUB_OF, OWNER_OF = lang.origin("hub").split()[-1], lang.origin("owner").split()[-1]   # the word after the preposition
RU_WORD, EN_WORD = (next(w for w, c in lang.legacy("lang_codes").items() if c == code) for code in ("ru", "en"))
# the earlier head of an owner's answer (a regex in the language data): with every optional word, and with none
ANSWER_FULL = re.sub(r"\[(\w)(\w)\]", r"\2", re.sub(r"\(\?:([^()]*)\)\?", r"\1", lang.legacy("head_answer")))
ANSWER_BARE = re.sub(r"\[(\w)(\w)\]", r"\1", re.sub(r"\(\?:[^()]*\)\?", "", lang.legacy("head_answer")))
HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


def launch_text(cmd):
    """What claude gets: the command line and the suffix file it names (the suffix goes as a file, PR #499)."""
    text = " ".join(cmd)
    if "--append-system-prompt-file" in cmd:
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            text += " " + f.read()
    return text


class Journal(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "dev", cwd="/d", session_id="s-d", role="agent", about="Dev", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **OWNER)
        for n in ("hub", "dev", "maps"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def last(self):
        return max(self.st["messages"].values(), key=lambda m: (m["created"], m["id"]))


class Sender(Journal):
    """from = {kind, name?} next to the text label origin — on everyone who writes a message."""

    def test_every_writer_names_the_sender_as_a_field(self):
        self.assertIsNone(self.ev("message.received", None, to="maps", text="check the map", **OWNER))
        self.assertEqual(self.last()["from"], {"kind": "owner"})
        self.assertIsNone(self.ev("message.received", None, to="dev", text="take a look", channel="session", owner=False,
                                  sender="maps"))
        self.assertEqual(self.last()["from"], {"kind": "session", "name": "maps"})
        self.assertIsNone(self.ev("message.received", None, to="maps", text="x", channel="plugin", owner=False,
                                  sender="mod:voice"))
        self.assertEqual(self.last()["from"], {"kind": "module", "name": "voice"})
        self.assertIsNone(self.ev("message.received", None, to="maps", text="x", channel="plugin", owner=False,
                                  sender="themes"))
        self.assertEqual(self.last()["from"], {"kind": "plugin", "name": "themes"})
        for origin, kind in ((forms.ORIGIN_CORE, "core"), (forms.ORIGIN_HUB, "hub"), (forms.ORIGIN_OWNER, "owner"),
                             (forms.ORIGIN_SCHEDULE, "schedule")):
            msg = reducer.new_message(self.st, f"m-{kind}", "maps", "t", 5000, origin=origin)
            self.assertEqual(msg["from"], {"kind": kind})
        # a label with a tail (addressed to a departed one…, an addition to a task) — the writer names the field itself
        self.assertNotIn("from", reducer.new_message(self.st, "m-tail", "maps", "t", 5000,
                                                     origin=forms.ORIGIN_CORE + ", x"))

    def test_the_task_and_its_answer_carry_the_sender(self):
        self.assertIsNone(self.ev("delegation.created", None, session="dev", context="fix the build", channel="session",
                                  owner=False, sender="maps"))
        d = next(iter(self.st["delegations"].values()))
        self.assertEqual(self.st["messages"][d["message"]]["from"], {"kind": "session", "name": "maps"})

    def test_views_believe_the_field(self):
        st = self.st
        said = lambda m: (views._sender(st, m), views._who(m))   # noqa: E731
        # field against text — the field
        self.assertEqual(said({"origin": forms.ORIGIN_OWNER, "from": {"kind": "core"}}), ("core", WHO["core"]))
        self.assertEqual(said({"origin": "", "from": {"kind": "hub"}}), ("hub", "hub"))
        self.assertEqual(said({"origin": forms.ORIGIN_CORE, "from": {"kind": "session", "name": "maps"}}), ("maps", "maps"))
        self.assertEqual(said({"origin": "", "from": {"kind": "module", "name": "voice"}}),
                         (MODULE_VOICE, WHO_VOICE))
        self.assertEqual(said({"origin": "", "from": {"kind": "schedule"}}), ("schedule", WHO["schedule"]))
        self.assertEqual(said({"origin": forms.ORIGIN_CORE, "from": {"kind": "owner"}}), ("owner", WHO["owner"]))
        # no field — the current text, as before
        self.assertEqual(said({"origin": forms.ORIGIN_CORE}), ("core", WHO["core"]))
        self.assertEqual(said({"origin": forms.ORIGIN_HUB}), ("hub", "hub"))
        self.assertEqual(said({"origin": forms.ORIGIN_SCHEDULE}), ("schedule", WHO["schedule"]))
        # the labels of the earlier Russian texts
        self.assertEqual(said({"origin": lang.origin("owner") + ", in reply to: «x»"}), ("owner", WHO["owner"]))
        self.assertEqual(said({"origin": f" {FROM} maps, reply to task d1"}), ("maps", "maps"))
        self.assertEqual(said({"origin": f" {FROM} {OLD_VOICE}"}), (MODULE_VOICE, WHO_VOICE))
        self.assertEqual(said({"origin": ""}), ("owner", WHO["core"]))
        self.assertEqual(said({"origin": ", an addition to task d5"}), (", an addition to task d5", WHO["core"]))
        # an unknown field value — text
        self.assertEqual(said({"origin": forms.ORIGIN_CORE, "from": {"kind": "alien"}}), ("core", WHO["core"]))
        self.assertEqual(said({"origin": forms.ORIGIN_CORE, "from": {"kind": "session"}}), ("core", WHO["core"]))   # no name
        # words are in their own language: a session named user/core in the current label is a session (review 2026-10-17)
        self.assertEqual(said({"origin": f" {FROM} user"}), ("user", "user"))
        self.assertEqual(said({"origin": f" {FROM} core"}), ("core", "core"))
        # the future text
        self.assertEqual(said({"origin": " from core"}), ("core", WHO["core"]))
        self.assertEqual(said({"origin": " from hub"}), ("hub", "hub"))
        self.assertEqual(said({"origin": " from schedule"}), ("schedule", WHO["schedule"]))
        self.assertEqual(said({"origin": " from user, replying to: «x»"}), ("owner", WHO["owner"]))
        self.assertEqual(said({"origin": " from maps, answer to task d1"}), ("maps", "maps"))
        self.assertEqual(said({"origin": " from module voice"}), (MODULE_VOICE, WHO_VOICE))
        self.assertEqual(said({"origin": " from plugin themes"}), (PLUGIN_THEMES, WHO_THEMES))

    def test_service_messages_are_known_by_the_field(self):
        core = {"origin": "", "from": {"kind": "core"}, "state": "queued", "session": "dev", "id": "m1"}
        self.assertTrue(forms.is_core(core))
        self.assertFalse(forms.is_core({"origin": forms.ORIGIN_CORE, "from": {"kind": "owner"}}))
        self.assertTrue(forms.is_core({"origin": forms.ORIGIN_CORE}))
        self.assertTrue(forms.is_core({"origin": " from core"}))
        self.assertTrue(forms.is_core({"origin": forms.ORIGIN_CORE, "from": {"kind": "alien"}}))
        self.assertFalse(forms.is_core({}))
        # the auto-executor of earlier journals (task.auto_run) does not take service items; it takes a task
        self.st["delegations"]["d1"] = {"id": "d1", "message": "m1", "session": "dev", "state": "working"}
        self.assertTrue(executors._takeable(self.st, {**core, "from": {"kind": "session", "name": "maps"}}, "dev"))
        self.assertFalse(executors._takeable(self.st, core, "dev"))
        self.assertFalse(executors._takeable(self.st, {**core, "from": None, "origin": " from hub"}, "dev"))

    def test_waiting_word_is_not_stale_after_a_core_note_known_by_the_field(self):
        self.st["sessions"]["maps"]["waiting"] = {"text": "waiting for a reply", "since": 1}
        reducer.new_message(self.st, "m-x", "maps", "t", 5000, origin="", frm={"kind": "core"})
        self.assertNotIn("stale", self.st["sessions"]["maps"]["waiting"])
        reducer.new_message(self.st, "m-y", "maps", "t", 5001, origin=forms.ORIGIN_CORE, frm={"kind": "owner"})
        self.assertIn("stale", self.st["sessions"]["maps"]["waiting"])

    def test_the_watch_believes_the_field(self):
        rule = watch.urgency_rule
        self.assertEqual(rule({"origin": "", "from": {"kind": "core"}, "text": "x"}), 0.0)
        self.assertIsNone(rule({"origin": forms.ORIGIN_CORE, "from": {"kind": "owner"}, "text": "make a sales report"}))
        status = f"{lang.word('watch', 'status_q')}?"   # a short status question in Russian
        self.assertEqual(rule({"origin": "", "from": {"kind": "owner"}, "text": status}), 1.0)
        self.assertEqual(rule({"origin": " from core", "text": "x"}), 0.0)
        self.assertEqual(rule({"origin": " from user", "text": status}), 1.0)
        note = f"Hub: {lang.word('watch', 'hub_service')} ms365 for you."   # a hub service note in Russian
        self.assertEqual(rule({"origin": "", "from": {"kind": "session", "name": "hub"}, "text": note}, "hub"), 0.0)
        self.assertEqual(rule({"origin": " from hub-core", "text": note}, "hub-core"), 0.0)
        # by the name field — whole: session hub-dev is not the hub session hub (by text, the Russian "from hub-dev" label started with "from hub")
        self.assertIsNone(rule({"origin": f" {FROM} hub-dev", "from": {"kind": "session", "name": "hub-dev"}, "text": note},
                               "hub"))


class Marker(unittest.TestCase):
    """The label in the turn text: [hub:mN <Russian "from"> X, …] and the future [hub:mN from X, …] — only the hook sees it (there is no field)."""

    def test_the_hook_takes_both_spellings(self):
        for head in (f"{lang.head('m5', kind='core')} x", "[hub:m5 from core] x",
                     "[hub:m5] x", "[hub:m5 from user, replying to: «a»] x"):
            kind, data = hub_hook.translate({"hook_event_name": "UserPromptSubmit", "prompt": head})
            self.assertEqual(data["marker"], "m5", head)
        kind, data = hub_hook.translate({"hook_event_name": "UserPromptSubmit",
                                         "prompt": "[hub:m5 from user] a\n[hub:m6 from maps] b"})
        self.assertEqual((data["marker"], data["also"]), ("m5", ["m6"]))

    def test_the_flow_reads_who_started_the_turn_in_both_spellings(self):
        for head, who in ((f"{lang.head('m5', kind='schedule')} [⏰ x] y", "schedule"),
                          ("[hub:m5 from schedule] [⏰ x] y", "schedule"),
                          ("[hub:m5 from user] y", "owner"), (f"{lang.head('m5', kind='owner')} y", "owner"),
                          ("[hub:m5] y", "owner"), ("[hub:m5 from core] y", "core"), ("[hub:m5 from hub] y", "hub"),
                          ("[hub:m5 from module voice] y", "core"), (f"{lang.head('m5', who=OLD_VOICE)} y", "core"),
                          ("[hub:m5 from plugin themes] y", "core"), ("[hub:m5 from maps, answer to task d1] y", "maps"),
                          (f"[hub:m5 {FROM} maps, answer to task d1] y", "maps"), (f"[hub:m5 {FROM} user] y", "user"),
                          (f"{lang.head('m5', who=HUB_OLD)} y", "hub"), (f"[hub:m5 from {HUB_OF}] y", HUB_OF)):
            self.assertEqual(flow._origins({"marker": "m5", "prompt_head": head}), (who, False), head)

    def test_the_watch_knows_scheduled_turns_and_wait_checks_in_both_spellings(self):
        t = lambda head: {"type": "turn.started", "ts": 0, "data": {"prompt_head": head}}   # noqa: E731
        for head in (f"{lang.head('m1', kind='schedule')} [⏰ schedule a] x", "[hub:m1 from schedule] [⏰ schedule a] x"):
            self.assertIn("schedule job", watch._line(t(head)))
            self.assertFalse(watch.plausible("hung", [t(head)], 6 * 60, {"state": "busy"}))
        for head in (f"{lang.head('m1', kind='schedule')} [⏰ schedule a-wait-ab12-1] {forms.WAIT_MARKS[0]} x. "
                     f"{lang.legacy('check_now')};",
                     "[hub:m1 from schedule] [⏰ schedule a-wait-ab12-1] [waiting for reply] x. Check now;"):
            self.assertIn("hub wait check", watch._line(t(head)))


class AppTail(Journal):
    """The client that sent the owner's message goes into the label: ORIGIN_OWNER + " · Android 0.2.7"."""

    def label(self, **data):
        self.assertIsNone(self.ev("message.received", None, to="maps", text="check", **OWNER, **data))
        return self.last()

    def test_the_label_names_the_client_and_stays_the_owner(self):
        msg = self.label(app="android", app_version="0.2.7")
        self.assertEqual((msg["origin"], msg["from"]), (forms.ORIGIN_OWNER + " · Android 0.2.7", {"kind": "owner"}))
        self.assertEqual(self.label(app="ios", app_version="2.14 (412)")["origin"],
                         forms.ORIGIN_OWNER + " · iOS 2.14 (412)")
        self.assertEqual(self.label(app="telegram")["origin"], forms.ORIGIN_OWNER + " · Telegram")
        self.assertEqual(self.label(app="web", app_version="1.6.1")["origin"], forms.ORIGIN_OWNER + " · Web 1.6.1")

    def test_without_a_client_or_with_a_bad_one_the_label_is_as_before(self):
        self.assertEqual(self.label()["origin"], forms.ORIGIN_OWNER)
        for bad in ({"app": "windows", "app_version": "1"}, {"app_version": "1"}, {"app": ["ios"]}):
            msg = self.label(**bad)
            self.assertEqual((msg["origin"], msg["from"]), (forms.ORIGIN_OWNER, {"kind": "owner"}), bad)
        # a good client with a bad version: the client stays, the version is dropped
        for ver in ("1,2", "]x", "1" * 21, " 1", 3, "1" + chr(0x3164) * 3):   # U+3164: a letter the eye does not see
            self.assertEqual(self.label(app="ios", app_version=ver)["origin"], forms.ORIGIN_OWNER + " · iOS", ver)

    def test_a_session_cannot_dress_up_as_a_client(self):
        self.assertIsNone(self.ev("message.received", None, to="dev", text="take a look", channel="session", owner=False,
                                  sender="maps", app="android", app_version="1"))
        self.assertEqual(self.last()["origin"], " from maps")

    def test_the_reply_tail_follows_the_client(self):
        msg = self.label(app="mac", app_version="3.1", quote="what?")
        self.assertEqual(msg["origin"], forms.ORIGIN_OWNER + " · Mac 3.1, in reply to: «what?»")

    def test_readers_take_the_word_before_the_tail(self):
        head = f"[hub:m5{forms.ORIGIN_OWNER} · Android 0.2.7] y"
        self.assertEqual(flow._origins({"marker": "m5", "prompt_head": head}), ("owner", False))
        self.assertEqual(flow._origins({"marker": "m5", "prompt_head": f"[hub:m5{lang.origin('owner')} · iOS 2.14 (412), answer to d1] y"}),
                         ("owner", False))
        self.assertEqual(forms.parse_origin(f"{lang.origin('owner').strip()} · iOS 2.14 (412), answer to d1"), ("owner", None))
        self.assertEqual(forms.parse_origin("from user · Web 1.6"), ("owner", None))
        self.assertEqual(forms.fixed_kind(forms.ORIGIN_OWNER + " · Mac 3.1"), "owner")
        self.assertEqual(forms.fixed_kind(forms.ORIGIN_HUB), "hub")
        self.assertIsNone(forms.fixed_kind(f" {FROM} maps"))
        kind, data = hub_hook.translate({"hook_event_name": "UserPromptSubmit", "prompt": head})
        self.assertEqual(data["marker"], "m5")
        msg = reducer.new_message(self.st, "m-app", "maps", "t", 5000, origin=forms.ORIGIN_OWNER + " · Mac 3.1")
        self.assertEqual(msg["from"], {"kind": "owner"})
        t = {"type": "turn.started", "ts": 0, "data": {"prompt_head": head}}
        self.assertNotIn("schedule", watch._line(t))


class Heads(Journal):
    """The executor name — by the task text without the core's headers (both forms of core/forms.HEADS)."""

    def test_the_name_is_the_one_older_code_gives(self):
        # review 2026-10-17: a name from a separate words field would diverge from the earlier code when folding the journal tail — the core
        # would start a second executor for the same task; the name goes by text, as before
        self.assertIsNone(self.ev("message.received", None, to="hub", text="deploy", **OWNER))
        mid = self.last()["id"]
        self.assertIsNone(self.ev("turn.started", "dev", marker=None, prompt_head="long work"))   # busy
        self.assertIsNone(self.ev("delegation.created", None, session="dev", **{"for": mid}, channel="session",
                                  owner=False, sender="hub"))
        d = next(iter(self.st["delegations"].values()))
        self.st["messages"][d["message"]]["body"] = "another"   # the core does not read the words fields
        self.assertIsNone(self.ev("message.routed", d["id"], route="executor", channel="session", owner=False,
                                  sender="hub"))
        self.assertEqual(list(self.st["runs"]), ["dev-deploy-zadacha"])

    def test_old_texts_and_future_heads_are_stripped(self):
        name = lambda text: executors.auto_name(reducer.empty(), "dev", text)   # noqa: E731
        # the words of the earlier headers whose data is a regex: "<voice>:\s*", "^\s*<Context>(?: ...)?:\s*"
        voice = lang.legacy("head_voice").split(":")[0]
        context = lang.legacy("head_context").removeprefix(r"^\s*").split("(")[0]
        for text in (f"{lang.legacy('head_task')} {OWNER_OF} (#m1). {lang.legacy('head_words')}:\n> 🎤 {voice}: fix map",
                     "Task from the user (#m1). Their words:\n> fix map",
                     "Task from the user (#m1). Their words:\n> 🎤 voice: fix map", "🎤 voice: fix map", "🎤 fix map",
                     f"🎤 {i18n.tr('ru', 'web.voice.head')}: fix map", "🎤 φωνή: fix map",   # the page's word in any language
                     f"{ANSWER_BARE} q8 (the question is stale — check):\n— fix map",
                     "The user's answer to question q8 (the question is stale):\n— fix map",
                     "This is the user's answer to your question q8 («what?»), passed on by the hub:\nfix map",
                     f"{ANSWER_FULL} q8 («what?»){lang.legacy('head_passed')}:\nfix map",
                     "Answer to question q8 («what?»): fix map",
                     f"{context}{lang.origin('hub')}: fix map", f"{lang.legacy('hub_word').capitalize()}, fix map",
                     "[hub:m3 from user] fix map"):
            self.assertEqual(name(text), "dev-fix-map", text)
        # the English text of a person — as in main (review 2026-10-17, second round): the headers of people and sessions are translated by their slices
        for text, was in (("Hub, fix the login page", "dev-hub-fix"), ("Context: login breaks", "dev-context-login"),
                          ("voice: fix login", "dev-voice-fix")):
            self.assertEqual(name(text), was, text)


class Wait(unittest.TestCase):
    """The `hub wait` check: what we wait for is the task's wait field; without it — the task text (both forms)."""

    def test_the_field_first_then_both_texts(self):
        w = schedule.waiting_for
        old = lambda what: f"{forms.WAIT_MARKS[0]} {what}. {lang.legacy('check_now')};"   # noqa: E731 — the earlier text
        self.assertEqual(w({"id": "a-wait-ab12-1", "wait": "a colleague's reply", "prompt": old("another")}),
                         "a colleague's reply")
        self.assertEqual(w({"id": "a-wait-ab12-1", "prompt": old("a colleague's reply") + " …"}),
                         "a colleague's reply")
        self.assertEqual(w({"id": "a-wait-ab12-1", "prompt": "[waiting for reply] a reply. Check now; …"}), "a reply")
        self.assertEqual(w({"id": "a-wait-ab12-1", "wait": 5, "prompt": old("x")}), "x")
        self.assertIsNone(w({"id": "nightly", "wait": "x", "prompt": old("x")}))

    def test_the_job_keeps_the_field_and_the_cli_writes_it(self):
        job = schedule.normalize({"id": "a-wait-ab12-1", "session": "a", "at": 2000, "prompt": "p", "wait": "reply"},
                                 1000, "UTC")
        self.assertEqual(job["wait"], "reply")
        # the field exists only on hub wait checks (review 2026-10-17)
        self.assertNotIn("wait", schedule.normalize({"id": "nightly", "session": "a", "at": 2000, "prompt": "p",
                                                     "wait": "x"}, 1000, "UTC"))
        state = tempfile.mkdtemp(prefix="forms-wait-")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "gp"}
        r = subprocess.run([sys.executable, HUB, "wait", "a colleague's reply in the chat", "--for", "1h"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        jobs = [ev for _, ev in spool.take(os.path.join(state, "spool")) if ev["type"] == "schedule.set"]
        self.assertTrue(jobs)
        self.assertTrue(all(e["data"]["wait"] == "a colleague's reply in the chat" for e in jobs))

    def test_setting_the_same_job_again_with_the_field_keeps_it(self):
        st = reducer.empty()
        ctx = {"new_message": reducer.new_message, "hub": None}
        for seq, extra in ((1, {}), (2, {"wait": "reply"})):
            ev = {"seq": seq, "ts": 1000 + seq, "type": "schedule.set", "entity": "a-wait-ab12-1", "data": {}}
            schedule.apply(st, ev, {"session": "a", "at": 5000, "prompt": "p", **extra, **OWNER}, ctx)
        self.assertEqual(st["schedule"]["a-wait-ab12-1"]["wait"], "reply")

    def test_the_schedule_view_hands_the_field_to_the_page(self):
        st = reducer.empty()
        st["schedule"]["a-wait-ab12-1"] = {"id": "a-wait-ab12-1", "session": "a", "state": "active", "kind": "at",
                                           "at": 2000, "until": 2000, "prompt": "p", "wait": "reply"}
        (row,) = views.schedule_view(st, now=1000)
        self.assertEqual(row["wait"], "reply")


class QuietCodes(unittest.TestCase):
    """Service feed lines — by the line code; without a code (archives) — by text."""

    def test_by_code_then_by_text(self):
        q = views.quiet_line
        self.assertTrue(q({"kind": "sys", "code": "created", "text": "session created"}))
        self.assertTrue(q({"kind": "sys", "code": "job", "job": "a-wait-ab12-1", "text": "scheduled"}))
        job_line = lang.legacy("job_line").split("|")[-1]   # the earlier "job <id>" line start
        quiet = lang.legacy("quiet_sys")[2]   # the earlier "session created"
        self.assertFalse(q({"kind": "sys", "code": "job", "job": "nightly", "text": f"{job_line}a-wait-ab12-1 …"}))
        self.assertTrue(q({"kind": "sys", "text": quiet}))
        self.assertTrue(q({"kind": "sys", "code": "alien", "text": quiet}))   # an unknown code — text
        self.assertFalse(q({"kind": "sys", "code": "created", "command": True, "text": "x"}))

    def test_the_lines_carry_their_codes(self):
        r = views.Reader()
        raw = [("session.requested", "a", {"session_id": "s", "cwd": "/w", **OWNER}),
               ("session.started", "a", {"session_id": "s"}),
               ("schedule.set", "a-wait-ab12-1", {"session": "a", "at": 99999, "prompt": "p", **OWNER}),
               ("host.done", "a", {"do": "compact", "result": "sent"}),
               ("session.started", "a", {"session_id": "s", "source": "resume"})]
        r.feed_all([{"seq": i, "ts": 1000 + i, "src": "test", "type": t, "entity": e, "data": d}
                    for i, (t, e, d) in enumerate(raw, 1)])
        codes = [(it.get("code"), it.get("job")) for it in r.timelines["a"] if it["kind"] == "sys"]
        self.assertEqual(codes, [("created", None), ("job", "a-wait-ab12-1"), ("compacting", None),
                                 ("resumed", None)])


class Reasons(unittest.TestCase):
    """Reason codes: a core failure, the gateway not responding, limits, "Waiting for the owner's yes", the bot header."""

    def test_a_core_crash_comes_with_its_code(self):
        st = reducer.empty()
        st["sessions"]["a"] = {"name": "a"}   # without the needed fields: the fold will fail on the event
        _, _, why = reducer.reduce(st, {"seq": 1, "ts": 1, "type": "session.renamed", "entity": "a", "data": {"to": "b"}})
        self.assertTrue(why.startswith("core failure"))
        self.assertEqual(forms.code_of(why), forms.CORE_FAILURE)
        _, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "no.such", "entity": None, "data": {}})
        self.assertIsNone(forms.code_of(why))

    def test_crashes_after_a_module_write_are_counted_by_code(self):
        st = {"core_written": {"crashes": 0}}
        crash = forms.PREFIXES[forms.CORE_FAILURE][0] + " z"   # the earlier text of a core failure
        for d in ({"reason": "x", "code": "core_failure"}, {"reason": "core failure on y"}, {"reason": crash},
                  {"reason": crash, "code": "no_answer"}, {"reason": crash, "code": "alien"},
                  {"reason": "against the rule"}):
            corewrite.folded(st, {"type": "core.rejected", "data": {"of": 1, **d}})
        self.assertEqual(st["core_written"]["crashes"], 4)   # field against text — the field; an unknown one — text
        corewrite.folded(st, {"type": "core.rejected", "data": {"of": 1, "reason": crash, "code": ["x"]}})
        self.assertEqual(st["core_written"]["crashes"], 5)   # a code that is not a string — text, no crash

    @unittest.skipUnless(gateway, "the built-in gateway is cut out of this tree")
    def test_gateway_no_answer_by_code(self):
        cfg = {"gateway": {"enabled": True}}
        level, line = gateway.summary(cfg, {}, {"on": False, "why": "silent for 30 s", "code": "no_answer"}, 0)
        self.assertEqual((level, line), ("bad", i18n.t("core.gateway.sum_no_answer", why="silent for 30 s")))
        level, line = gateway.summary(cfg, {}, {"on": False, "why": "not answering for 30 s"}, 0)
        self.assertEqual(level, "bad")
        level, line = gateway.summary(cfg, {}, {"on": False, "why": forms.PREFIXES[forms.NO_ANSWER][0] + " 30 s"}, 0)
        self.assertEqual(level, "bad")

    def test_the_core_writes_the_gateway_code(self):
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "gateway.changed", "entity": None,
                                        "data": {"on": False, "channel": "core", "why": "x", "failed": True,
                                                 "code": "no_answer"}})
        self.assertIsNone(why)
        self.assertEqual(st["gateway"]["code"], "no_answer")

    def test_the_budget_reasons_start_as_the_prefixes(self):
        # a queued request an earlier core wrote without why_code is told by the start of its text: the English
        # reasons start exactly with the English prefixes
        for phrase, code in ((budget.POOL_RESET, forms.POOL_LIMIT), (budget.POOL_WORD, forms.POOL_LIMIT),
                             (budget.ECONOMY_WORD, forms.ECONOMY)):
            self.assertEqual(forms.reason_code(None, phrase.format(why="x"), forms.WAIT_CODES), code, phrase)

    def test_limits_by_code(self):
        self.assertEqual(budget.label("whatever", "pool_limit"), "pool limit — waits for your word")
        self.assertEqual(budget.label(f"{lang.legacy('prefixes')['pool_limit']} (x): …"), "pool limit — waits for your word")
        self.assertEqual(budget.label("pool limit (x): …"), "pool limit — waits for your word")
        self.assertEqual(budget.label("limit economy (x): …"), "limit economy — waits for the window to reset or your word")
        economy = forms.PREFIXES[forms.ECONOMY][0] + " (x)"   # the earlier text of limit economy
        self.assertIsNone(budget.label(economy, "weekly_limit"))   # field against text — the field
        st = {"runs": {"a-x": {"minihub": True, "state": "queued", "why": "weekly limit 90%", "ts": 1, "by": "a"},
                       "a-y": {"minihub": True, "state": "queued", "why": "x", "why_code": "weekly_limit", "ts": 2,
                               "by": "a"},
                       "a-z": {"minihub": True, "state": "queued", "why": forms.PREFIXES[forms.WEEKLY_LIMIT][0] + " 90%",
                               "why_code": "alien", "ts": 3, "by": "a"}}, "sessions": {}}
        self.assertEqual([q["why"] for q in minihub.queue(st)], [i18n.t("core.minihub.queue_weekly")] * 3)

    def test_a_run_raised_out_of_the_queue_drops_the_code(self):
        t = Journal("setUp")
        t.setUp()
        t.st["limits"] = {"windows": {"seven_day": {"used": 0.9}}}
        self.assertIsNone(t.ev("task.run_requested", "dev-x", task="fix", channel="session", owner=False, sender="dev"))
        self.assertEqual(t.st["runs"]["dev-x"]["why_code"], "weekly_limit")
        self.assertIsNone(t.ev("task.run_go", "dev-x", **OWNER))
        self.assertEqual(t.st["runs"]["dev-x"]["state"], "ready")
        self.assertNotIn("why_code", t.st["runs"]["dev-x"])
        self.assertNotIn("why_l10n", t.st["runs"]["dev-x"])

    def test_a_queued_run_keeps_the_code_of_its_wait(self):
        st = reducer.empty()
        st["limits"] = {"windows": {"seven_day": {"used": 0.9}}}
        why = executors.budget_waits(st)
        self.assertEqual(forms.code_of(why), forms.WEEKLY_LIMIT)
        got = forms.why_fields(why)
        self.assertEqual({k: v for k, v in got.items() if k != "why_l10n"}, {"why": str(why), "why_code": "weekly_limit"})
        self.assertIs(type(got["why"]), str)
        # the marks of the core's phrase go next to it: the Russian owner reads the reason in Russian, the state holds
        # the English (hub task queue, hub session wake)
        self.assertTrue(got["why"].startswith("weekly limit"))
        self.assertEqual(coretext.human(coretext.got(got, "why"), "ru"), coretext.human(why, "ru"))
        self.assertNotEqual(coretext.human(coretext.got(got, "why"), "ru"), got["why"])
        self.assertEqual(forms.why_fields(None), {"why": None})
        self.assertEqual(forms.why_fields("a plain reason"), {"why": "a plain reason"})

    def test_the_yes_note_is_dropped_by_the_field(self):
        st = reducer.empty()
        st["sessions"]["a"] = {"name": "a", "state": "idle"}
        st["delegations"]["d1"] = {"id": "d1", "session": "a", "state": "working", "created": 1, "words": None,
                                   "for": None, "by": "hub", "context": "fix\nSpecial line\nWaiting for the owner's yes: x"}
        did, text = delegations.open_task(st, "a")
        self.assertIn("Special line", text)
        self.assertNotIn("Waiting for the owner", text)
        st["delegations"]["d1"]["yes_note"] = "Special line"
        did, text = delegations.open_task(st, "a")
        self.assertNotIn("Special line", text)
        self.assertIn("Waiting for the owner", text)   # field against text — the field
        st["delegations"]["d1"].update(yes_note="Special\nline", context="fix\nSpecial\nline")
        self.assertNotIn("Special", delegations.open_task(st, "a")[1])   # a multi-line string — taken whole

    def test_the_yes_note_is_written_with_the_task(self):
        t = Journal("setUp")
        t.setUp()
        t.st["sessions"]["dev"]["requests"] = [{"tool": "ms365", "state": "needs-owner"}]
        self.assertIsNone(t.ev("delegation.created", None, session="dev", context="fix", channel="session",
                               owner=False, sender="hub"))
        d = next(iter(t.st["delegations"].values()))
        self.assertTrue(d["yes_note"].startswith("Waiting for the owner's yes: ms365"))
        self.assertTrue(d["context"].endswith(d["yes_note"]))
        # the core does not take a foreign field in an event (review 2026-10-17): otherwise open_task would drop the task line
        t.st["sessions"]["maps"]["requests"] = []
        self.assertIsNone(t.ev("delegation.created", None, session="maps", context="Check prod", yes_note="Check prod",
                               channel="session", owner=False, sender="hub"))
        d = max(t.st["delegations"].values(), key=lambda x: x["created"])
        self.assertNotIn("yes_note", d)

    def test_the_bot_drops_its_own_head_by_the_route(self):
        reply = lambda text, route: tg.Bot.quote({"reply_to_message": {"text": text}}, route)   # noqa: E731
        self.assertEqual(reply("maps answers:\nthe map is ready", {"session": "maps", "head": "maps answers:"}), "the map is ready")
        old = lang.legacy("reply_head").format(name="maps")   # the earlier head of the bot
        self.assertEqual(reply(f"{old}\nthe map is ready", {"session": "maps", "head": "maps answers:"}),
                         f"{old}\nthe map is ready")   # field against form — the field
        for head in ("maps:", old, "maps replies:"):
            self.assertEqual(reply(f"{head}\nthe map is ready", {"session": "maps"}), "the map is ready")

    def test_the_bot_keeps_the_head_it_wrote(self):
        from modules.telegram import fmt
        self.assertEqual(fmt.plain("<b>a&amp;b</b> replies:"), "a&b replies:")


class Texts(unittest.TestCase):
    def test_both_session_headings_in_changes(self):
        old = lang.legacy("session_section")
        text = f"## 2026-10-17\n- visible\n### For sessions\n- new rule\n## 2026-10-16\n{old}\n- old rule\n"
        self.assertEqual(news.session_notes(text), ["new rule", "old rule"])
        self.assertEqual(news.changes(text), [{"date": "2026-10-17", "items": ["visible"]}, {"date": "2026-10-16", "items": []}])
        from tools import release
        self.assertEqual(release.SESSIONS.split("a\n### For sessions\nb")[0], "a\n")
        self.assertEqual(release.SESSIONS.split(f"a\n{old}\nb")[0], "a\n")

    def test_lessons_heading_in_english_with_for(self):
        self.assertEqual(lessons.scan("Result.\n\nLessons (for CLAUDE.md):\n- lesson one"), ["yes", "- lesson one"])
        # an ordinary phrase is not a header (review 2026-10-17)
        self.assertNotEqual(lessons.scan("Lessons for the reviewer are already in the MR description.\nfurther")[0], "yes")


class Language(unittest.TestCase):
    """The owner's language is a code (ru, en); earlier words in the state read as codes; the rule for sessions uses the same word."""

    def test_codes_and_old_words(self):
        # a Russian sentence of the data's words ("check now, waiting for a reply"; a Russian-only letter in it)
        self.assertEqual(reducer.owner_lang(f"{lang.legacy('check_now')}, {lang.legacy('wait')}"), "ru")
        # since languages (slice C1) Latin is "latin": the language is not known (spec §4.2); the earlier spelled-out language words and en are the same
        self.assertEqual(reducer.owner_lang("Please check the build, it fails again"), "latin")
        st = reducer.empty()
        st["owner"].update(lang=RU_WORD, langs=[RU_WORD, EN_WORD, RU_WORD])
        reducer._note_lang(st, "Please check the build, it fails again")
        self.assertEqual(st["owner"]["langs"], ["ru", "latin", "ru", "latin"])
        self.assertEqual(st["owner"]["lang"], "latin")   # a tie — the last one, as before
        st = reducer.empty()
        st["owner"]["lang"] = EN_WORD
        self.assertEqual(reducer.known_lang(st), "en")
        for word, code in ((RU_WORD, "ru"), ("ru", "ru"), (EN_WORD, "en"), ("en", "en"), (None, None),
                           ("latin", "en"), ("uk", "uk"), ("ja", "ja"), ("qq", None)):
            r = views.Reader()
            r.state["owner"]["lang"] = word
            self.assertEqual(r.snapshot()["owner"]["lang"], code)

    def test_the_rule_names_the_language_in_words_as_before(self):
        from hosts.streamjson import StreamHost
        host = StreamHost(tempfile.mkdtemp(prefix="forms-lang-"), claude=("true",))
        for code in ("ru", RU_WORD, "Russian"):
            cmd = launch_text(host.command("worker", "s-w", lang=code))
            self.assertIn("The owner's language is Russian", cmd)
        for code in ("en", "English"):
            self.assertIn("The owner's language is English", launch_text(host.command("worker", "s-w", lang=code)))


class Contract(unittest.TestCase):
    """Future forms (written by the next slices) are only these, and the real parser of this slice understands each of them."""

    def test_every_future_form_is_understood(self):
        st = reducer.empty()
        for origin, sender in ((" from core", "core"), (" from hub", "hub"), (" from user", "owner"),
                               (" from schedule", "schedule"), (" from module voice", MODULE_VOICE),
                               (" from plugin themes", PLUGIN_THEMES), (" from maps", "maps")):
            self.assertEqual(views._sender(st, {"origin": origin}), sender, origin)
        for head in ("[hub:m5 from core] x", "[hub:m5 from user, replying to: «a»] x", "[hub:m5 from schedule] x"):
            self.assertEqual(hub_hook.translate({"hook_event_name": "UserPromptSubmit", "prompt": head})[1]["marker"],
                             "m5", head)
        for text in ("Task from the user (#m1). Their words:\n> fix map",
                     "The user's answer to question q8:\n— fix map",
                     "Answer to question q8 («what?»): fix map",
                     "This is the user's answer to your question q8 («what?»), passed on by the hub:\nfix map"):
            self.assertEqual(executors.auto_name(st, "dev", text), "dev-fix-map", text)
        self.assertEqual(schedule.waiting_for({"id": "a-wait-ab12-1", "prompt": "[waiting for reply] X. Check now; …"}),
                         "X")
        for code, prefix in forms.PREFIXES.items():
            self.assertTrue(forms.is_reason(code, None, prefix[1] + " …"), code)
            self.assertEqual(forms.reason_code(None, prefix[1] + " …", tuple(forms.PREFIXES)), code)
        st["sessions"]["a"] = {"name": "a", "state": "idle"}
        st["delegations"]["d1"] = {"id": "d1", "session": "a", "state": "working", "created": 1, "words": None,
                                   "for": None, "by": "hub", "context": "fix\nWaiting for the owner's yes: x"}
        self.assertNotIn("Waiting", delegations.open_task(st, "a")[1])
        self.assertEqual(tg.Bot.quote({"reply_to_message": {"text": "maps replies:\nready"}}, {"session": "maps"}),
                         "ready")
        self.assertEqual(news.session_notes("## 2026-10-18\n### For sessions\n- rule\n"), ["rule"])
        self.assertEqual([forms.lang_code(x) for x in ("ru", "en")], ["ru", "en"])
        self.assertEqual(lessons.scan("Result.\n\nLessons (for CLAUDE.md):\n- lesson one")[0], "yes")

    def test_the_list_is_what_the_parsers_know(self):
        self.assertEqual(forms.FROM_KINDS, ("owner", "session", "hub", "core", "schedule", "module", "plugin", "member"))
        self.assertEqual(set(forms.CORE_ORIGINS + forms.HUB_ORIGINS + forms.OWNER_ORIGINS),
                         {lang.origin("core"), " from core", lang.origin("hub"), " from hub", lang.origin("owner"),
                          " from user"})
        self.assertEqual(forms.MARKER_BY, (f" {FROM} ", " from "))
        self.assertEqual(forms.WAIT_MARKS, (f"[{lang.legacy('wait')}]", "[waiting for reply]"))
        self.assertEqual({c: p[1] for c, p in forms.PREFIXES.items()},
                         {"core_failure": "core failure", "no_answer": "not answering", "weekly_limit": "weekly limit",
                          "pool_limit": "pool limit", "economy": "limit economy"})
        self.assertEqual(forms.YES_NOTE_HEADS[1], "Waiting for the owner's yes")
        self.assertEqual(forms.REPLY_HEADS[2], "{name} replies:")
        self.assertEqual(forms.SESSION_SECTIONS, (lang.legacy("session_section"), "### For sessions"))
        self.assertEqual(set(forms.LANG_WORDS), {"ru", "en"})
        self.assertEqual(forms.WORDS["from"], {"core": "core", "hub": "hub", "user": "owner", "schedule": "schedule",
                                               "group member": "member"})
        # a member of the hub's Telegram group: the label reads back as such, never as a session or the owner
        self.assertEqual(forms.parse_origin(forms.ORIGIN_MEMBER + ", in reply to: «x»"), ("member", None))
        self.assertEqual(forms.fixed_kind(forms.ORIGIN_MEMBER), "member")
        self.assertEqual(forms.NAMED_WORDS["from"],
                         (("module ", "module"), ("plugin ", "plugin"), ("session ", "session")))


if __name__ == "__main__":
    unittest.main()
