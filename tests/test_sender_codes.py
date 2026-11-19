"""The page reads the core's senders as codes (slice C4): :core, :schedule, :owner (no session name has a ":"),
mod:<name> / ext:<name>. An archive parse made by an earlier core keeps the Russian words of then, and the state keeps a
job's `by` the way earlier cores compare it: the server gives the page codes for both, so the page holds no words of a
language. The core writes English labels, and a session whose name is a label word does not read as that sender."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import flow, forms, i18n, reducer, views
from hooks import hub_hook
from tests.fakes import lang

OLD = lang.legacy("who")
OWN = {"channel": "cli", "owner": True}


class ArchiveRows(unittest.TestCase):
    def test_an_earlier_parse_reads_as_the_codes(self):
        rows = [{"kind": "in", "channel": "core", "sender": OLD["core"]},
                {"kind": "in", "channel": "core", "sender": OLD["schedule"]},
                {"kind": "in", "channel": "core", "sender": OLD["owner"]},
                {"kind": "in", "channel": "core", "sender": OLD["module"].format(name="voice")},
                {"kind": "in", "channel": "core", "sender": OLD["plugin"].format(name="desk")},
                {"kind": "in", "channel": "session", "sender": "maps"},
                {"kind": "out", "text": "x"}, {"kind": "in", "sender": None}, "not a row"]
        got = [it.get("sender") if isinstance(it, dict) else it for it in views.sender_now(rows)]
        self.assertEqual(got, [":core", ":schedule", ":owner", "mod:voice", "ext:desk", "maps", None, None, "not a row"])

    def test_the_parse_of_this_core_stays_as_it_is(self):
        rows = [{"kind": "in", "sender": s} for s in (":core", ":schedule", ":owner", "mod:voice", "ext:desk", "hub",
                                                      "core", "owner", "a")]
        self.assertEqual([it["sender"] for it in views.sender_now([dict(r) for r in rows])], [r["sender"] for r in rows])
        self.assertEqual(views.sender_now(None), None)   # a broken piece of a parse: nothing to read

    def test_a_core_message_row_names_its_sender_by_code(self):
        st = reducer.empty()
        for origin, sender in ((forms.ORIGIN_CORE, ":core"), (lang.origin("core"), ":core"),
                               (forms.ORIGIN_SCHEDULE + ", job x", ":schedule"), (lang.origin("schedule"), ":schedule"),
                               (" from module voice", "mod:voice"),
                               (f" {lang.legacy('from')} {OLD['module'].format(name='voice')}", "mod:voice"),
                               (" from plugin desk", "ext:desk"), (" from maps, reply to task d1", "maps"),
                               (" from session user", "user"), (" from session core", "core")):
            msg = {"id": "m1", "text": "t", "origin": origin}
            self.assertEqual(views.core_row(msg, 1, 1.0, st)["sender"], sender, origin)

    def test_hub_show_names_the_codes_in_the_readers_words(self):
        for code, key, args in ((":core", "cli.show.core", {}), (":schedule", "cli.show.schedule", {}),
                                (":owner", "cli.show.owner", {}), ("mod:voice", "cli.show.module", {"name": "voice"}),
                                ("ext:desk", "cli.show.plugin", {"name": "desk"})):
            self.assertEqual(views.sender_word(code), i18n.t(key, **args), code)
        self.assertEqual(views.sender_word("maps"), "maps")
        line = views._item_text({"kind": "in", "sender": ":core", "text": "x"})
        self.assertTrue(line.startswith(i18n.t("cli.show.core") + ": "), line)


class Jobs(unittest.TestCase):
    def job(self, **kw):
        st, _, _ = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1000.0, "type": "schedule.set", "entity": "x",
                                                    "data": {"session": "a", "every": "1h", "prompt": "check", **OWN}})
        st["schedule"]["x"].update(kw)   # as an earlier core or a `hub wait` left it in the state
        return views.schedule_view(st, now=1001.0)[0]

    def test_who_set_it_is_a_code(self):
        self.assertEqual(self.job()["by"], ":owner")   # the state keeps the word earlier cores compare
        self.assertEqual(self.job(by=OLD["core"])["by"], ":core")
        self.assertEqual(self.job(by="owner")["by"], "owner")   # a session named owner stays a session
        self.assertEqual(self.job(by="maps")["by"], "maps")

    def test_a_wait_check_gives_its_gist_in_any_spelling(self):
        old = f"[{lang.legacy('wait')}] the answer of maps. {lang.legacy('check_now')}; then x"
        self.assertEqual(self.job(prompt=old)["wait_gist"], f"[{lang.legacy('wait')}] the answer of maps")
        new = "[waiting for reply] the answer of maps. Check now; then x"
        self.assertEqual(self.job(prompt=new)["wait_gist"], "[waiting for reply] the answer of maps")
        bare = f"[{lang.legacy('wait')}] the answer of maps, a note added"   # the head without the tail: whole
        self.assertEqual(self.job(prompt=bare)["wait_gist"], bare)
        odd = "[waiting for reply] the answer of maps. Check now: then x"   # the tail in another form: cut there
        self.assertEqual(self.job(prompt=odd)["wait_gist"], "[waiting for reply] the answer of maps")
        self.assertNotIn("wait_gist", self.job(prompt="check the build"))


class Labels(unittest.TestCase):
    """The core writes the labels in English; the hook, the "Flow" and the views read both spellings."""

    def test_what_the_core_writes(self):
        self.assertEqual((forms.ORIGIN_CORE, forms.ORIGIN_HUB, forms.ORIGIN_OWNER, forms.ORIGIN_SCHEDULE),
                         (" from core", " from hub", " from user", " from schedule"))
        st = reducer.empty()
        for data, origin in (({"channel": "session", "sender": "maps"}, " from maps"),
                             ({"channel": "plugin", "sender": "mod:voice"}, " from module voice"),
                             ({"channel": "plugin", "sender": "desk"}, " from plugin desk"),
                             ({"schedule_job": "j1"}, " from schedule, job j1"),
                             ({"owner": True}, " from user")):
            self.assertEqual(reducer._origin(data)[0], origin, data)
        self.assertEqual(reducer.new_message(st, "m1", "maps", "t", 1.0, origin=forms.ORIGIN_CORE)["from"],
                         {"kind": "core"})

    def test_a_session_named_as_a_label_word_is_still_a_session(self):
        """Russian "from" kept a session named user apart from the owner; the English label has the same words, so
        such a session goes as "from session <name>" — the owner's word cannot be dressed up by a session's name."""
        ordinary = {"sessions": {"hub": {"kind": None}}}   # an ordinary session named hub (the hub is another one)
        for name in ("user", "core", "schedule", "hub"):
            origin = reducer._origin({"channel": "session", "sender": name}, st=ordinary)[0]
            self.assertEqual(origin, f" from session {name}")
            self.assertEqual(forms.parse_origin(origin), ("session", name))
            self.assertIsNone(forms.fixed_kind(origin))
            head = f"[hub:m5{origin}] deploy prod"
            self.assertEqual(flow._origins({"marker": "m5", "prompt_head": head})[0], name)
            kind, data = hub_hook.translate({"hook_event_name": "UserPromptSubmit", "prompt": head})
            self.assertEqual(data["marker"], "m5")
        for st in ({"sessions": {"hub": {"kind": "hub"}}}, {"sessions": {}}):   # the hub session, or no session hub
            self.assertEqual(reducer._origin({"channel": "session", "sender": "hub"}, st=st)[0], " from hub")
        for name in ("module", "plugin", "session", "users", "maps"):   # not a word: as it is
            self.assertEqual(reducer._origin({"channel": "session", "sender": name})[0], f" from {name}")
            self.assertEqual(forms.parse_origin(f" from {name}"), ("session", name))

    def test_the_old_labels_still_read(self):
        for origin, kind in ((lang.origin("core"), "core"), (lang.origin("hub"), "hub"), (lang.origin("owner"), "owner"),
                             (lang.origin("schedule"), "schedule")):
            self.assertEqual(forms.fixed_kind(origin), kind)
            self.assertEqual(reducer.new_message(reducer.empty(), "m1", "a", "t", 1.0, origin=origin)["from"],
                             {"kind": kind})


class EarlierLabelsThroughTheCore(unittest.TestCase):
    """A hub updates with messages an earlier core labelled in Russian still queued: the new core types them with
    their labels, the hook takes the turn as theirs, the message is delivered and the feed names who sent it."""

    def test_queued_russian_labels_are_delivered_and_named(self):
        self.st, self.seq = reducer.empty(), 0
        for name, kind in (("hub-core", "hub"), ("maps", None)):
            self.ev("session.requested", name, cwd=f"/home/x/{name}", session_id=f"s-{name}",
                    **({"kind": kind} if kind else {}), **OWN)
            self.ev("session.started", name)
        self.ev("turn.started", "maps", marker=None)   # busy: what comes now waits in its queue
        self.ev("message.received", None, to="maps", text="check the tiles", **OWN)
        self.ev("message.received", None, to="maps", text="the geocoder is updated", channel="session", owner=False,
                sender="hub-core")
        old = {f"m{self.seq - 1}": lang.origin("owner"), f"m{self.seq}": f" {lang.legacy('from')} hub-core"}
        for mid, origin in old.items():   # as an earlier core stored them: the Russian label, no from field
            self.st["messages"][mid]["origin"] = origin
            self.st["messages"][mid].pop("from", None)
        acts = self.ev("turn.result", "maps", text="busy result", subtype="success", turns=1)
        for mid in old:
            typed = [a["text"] for a in acts if a.get("do") == "type"]
            self.assertTrue(typed, (mid, acts))
            self.assertTrue(typed[-1].startswith(f"[hub:{mid}{old[mid]}] "), typed[-1])
            kind, data = hub_hook.translate({"hook_event_name": "UserPromptSubmit", "prompt": typed[-1]})
            self.assertEqual(data["marker"], mid)
            self.ev(kind, "maps", **data)
            acts = self.ev("turn.result", "maps", text="done", subtype="success", turns=1)
            self.assertEqual(self.st["messages"][mid]["state"], "delivered", mid)
        first, second = (self.st["messages"][m] for m in old)
        self.assertEqual((views._sender(self.st, first), forms.sender(first)), ("owner", None))
        self.assertEqual(views._who(second), "hub-core")

    def ev(self, t, e, **d):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000.0 + self.seq, "type": t, "entity": e,
                                                      "data": d})
        self.assertIsNone(why, (t, why))
        return acts


if __name__ == "__main__":
    unittest.main()
