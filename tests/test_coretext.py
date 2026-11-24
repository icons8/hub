"""The core's words to sessions in the owner's language (core/coretext.py, task 06.11: a Russian owner saw "A hub restart
cut off your turn…" in the feed): the session gets the same English as before, the feed row carries the same text in the
hub's language as its label."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest

from core import board, coretext, daemon, delegations, executors, i18n, journal, owner, reducer, spool, views

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = {"channel": "web", "owner": True}
NOTHING = delegations.NOTHING   # the word the core recognizes in an answer, in any language


def ru(key, **args):
    return i18n.tr("ru", key, **args)


def catalog(lang):
    with open(os.path.join(REPO, "i18n", f"{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


class Catalog(unittest.TestCase):
    def test_every_phrase_is_the_english_of_its_catalog_text_and_has_a_russian_one(self):
        # the English stays in the code (the session's prompt is read there); the catalog keeps the same text, else the
        # owner's text would be translated from another English, or the span would not be recognized
        en, ru = catalog("en"), catalog("ru")
        self.assertGreaterEqual(len(coretext.TEMPLATES), 20)
        for key, english in coretext.TEMPLATES.items():
            with self.subTest(key=key):
                self.assertEqual(en.get(key), english)
                self.assertIn(key, ru)

    def test_the_session_gets_the_english_it_got_before(self):
        # the constants are still plain English strings: an eval reads them from the source, the session gets them
        from evals import prompts
        for mod, name in ((reducer, "CUT_TURN"), (reducer, "CUT_CRASH"), (reducer, "LIMIT_RESET"), (reducer, "PROMISE_NUDGE"),
                          (board, "HANDED_HEAD"), (owner, "MORE_HINT"), (executors, "UNMERGED")):
            with self.subTest(name=name):
                with open(mod.__file__, encoding="utf-8") as f:
                    self.assertEqual(prompts.constant(f.read(), name), str(getattr(mod, name)))
        # the word the core recognizes in an answer is a fixed substitution, not catalog text (en.json stays Latin)
        self.assertTrue(str(reducer.CUT_TURN).endswith(f"answer with one line «{NOTHING}»."))
        self.assertNotIn(NOTHING, catalog("en")["core.msg.cut_turn"])
        self.assertTrue(str(reducer.CUT_CRASH).endswith(f"answer with one line «{NOTHING}»."))
        self.assertNotIn(NOTHING, catalog("en")["core.msg.cut_crash"])
        self.assertNotIn("{", str(reducer.CUT_TURN))


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("pe", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, actions, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                         "entity": entity, "data": data})
        self.assertIsNone(why)
        return actions

    def cut(self, **background):
        if background:
            self.ev("session.background", "pe", tasks=[{"id": "a1", "kind": "agent", "title": "Review"}])
        else:
            self.ev("turn.started", "pe", marker=None)
        self.ev("session.exited", "pe", why="reconcile")
        actions = self.ev("session.started", "pe")
        (msg,) = [m for m in self.st["messages"].values() if m["session"] == "pe"]
        return msg, actions

    def test_a_cut_turn_goes_to_the_session_in_english_and_to_the_owner_in_russian(self):
        msg, actions = self.cut()
        self.assertEqual(msg["text"], str(reducer.CUT_TURN))
        self.assertIs(type(msg["text"]), str)   # the state holds plain strings
        (typed,) = [a for a in actions if a.get("do") == "type"]
        self.assertIn("A hub restart cut off your turn", typed["text"])
        self.assertNotIn(ru("core.msg.cut_turn")[:20], typed["text"])
        self.assertEqual(coretext.shown(msg, "ru"), ru("core.msg.cut_turn", nothing=NOTHING))
        self.assertIsNone(coretext.shown(msg, "en"))
        json.dumps(self.st)   # the marks are plain data

    def test_a_russian_tail_built_onto_a_phrase_stays_as_it_is(self):
        msg, _ = self.cut(background=True)
        shown = coretext.shown(msg, "ru")
        self.assertTrue(shown.startswith(ru("core.msg.cut_turn", nothing=NOTHING) + " "), shown)
        self.assertIn("Review", shown)
        self.assertTrue(msg["text"].startswith("A hub restart cut off"))

    def test_the_feed_row_has_the_label_in_the_hubs_language_and_the_text_as_the_session_read_it(self):
        msg, _ = self.cut()
        i18n.use("ru")   # a Russian hub
        try:
            row = views.core_row(msg, 9, 109, self.st)
        finally:
            i18n.use("en")
        self.assertEqual(row["text"], msg["text"])
        self.assertEqual(row["label"], ru("core.msg.cut_turn", nothing=NOTHING))
        self.assertNotIn("label", views.core_row(msg, 9, 109, self.st))

    def test_a_relayed_reply_tail_is_shown_in_russian_after_the_hubs_own_words(self):
        # the hub passes the owner's Telegram words on: the core adds where the result goes to the hub's message
        self.st["sessions"]["hub"]["state"] = "idle"
        e = self.seq + 1
        self.ev("message.received", None, to="hub", text="check the logs", channel="telegram", owner=True,
                tg={"id": 5, "chat": 1})
        self.ev("message.received", None, to="pe", text="Logs: look at the errors", channel="session", owner=False,
                sender="hub", **{"for": f"m{e}"})
        msg = self.st["messages"][f"m{self.seq}"]
        self.assertIn(str(reducer.RELAYED_TAIL), msg["text"])
        shown = coretext.shown(msg, "ru")
        self.assertEqual(shown, "Logs: look at the errors\n\n" + ru("core.msg.relayed_tail"))

    def test_the_cores_task_lines_are_shown_to_the_owner_without_the_tag_and_the_numbers(self):
        # the session reads "REPLIED d8: a replied to the owner on m5: "…"": the tag and the numbers are for it (hub show
        # m5, --about d8); the owner reads only the words — in English too, and the quoted reply as it is (T(shown=…))
        quote = "see q3 and the d2 branch: done, run the m4 job"
        owner = delegations.TO_OWNER.format(mid="m5")
        to_b = delegations.SESSION_WORD.format(name="b")
        lines = [
            (delegations.REPLIED.format(id="d8", name="a", target=owner, short=quote),
             f'a replied to the owner: "{quote}"',
             ru("core.delegations.replied.shown", name="a", target=ru("core.delegations.to_owner.shown"), short=quote)),
            (delegations.REPLIED_MORE.format(id="d8", name="a", target=to_b, short="x"),
             'a replied to session b again: "x"',
             ru("core.delegations.replied_more.shown", name="a", target=ru("core.delegations.session_word", name="b"),
                short="x")),
            (delegations.STOPPED_HUB.format(id="d7", name="b", cut=delegations.CUT_ERROR),
             "b — the turn broke off with an error, no reply to the owner",
             ru("core.delegations.stopped_hub.shown", name="b", cut=ru("core.delegations.cut_error"))),
            (delegations.TIMEOUT_HUB.format(id="d6", name="a", minutes=90),
             "a has worked on the task longer than 90 minutes",
             ru("core.delegations.timeout.shown", name="a", minutes=90)),
        ]
        for text, english, russian in lines:
            self.assertRegex(str(text), r"^(REPLIED|STOPPED|TIMEOUT) d\d+", text)   # the session's form stays
            msg = {"id": "m9", "text": str(text), "l10n": coretext.marks(text)}
            self.assertEqual(coretext.shown(msg, "en"), english)
            self.assertEqual(coretext.shown(msg, "ru"), russian)
            self.assertNotRegex(russian.replace(quote, ""), r"\b[dmq]\d+\b")   # the numbers of the quote are its own
            self.assertEqual(views.core_row(msg, 9, 109, self.st)["label"], english)   # an English hub has the label too
        # a line a session acts on keeps its numbers and commands: no shown variant
        text = delegations.STOPPED_SESSION.format(id="d7", name="a", cut=delegations.CUT_ERROR,
                                                  how=delegations.HOW_SEND.format(name="a"))
        msg = {"text": str(text), "l10n": coretext.marks(text)}
        self.assertIsNone(coretext.shown(msg, "en"))
        self.assertTrue(coretext.shown(msg, "ru").startswith("STOPPED d7: "), coretext.shown(msg, "ru"))
        self.assertIn("hub show a", coretext.shown(msg, "ru"))

    def test_every_shown_variant_is_in_the_catalogs_with_no_tag_and_no_number(self):
        # T(shown=…) registers the variant under <key>.shown: en.json has the text of the code, ru.json a translation,
        # and neither carries the session's tag or the {id} and {mid} numbers
        keys = [k for k in coretext.TEMPLATES if k.endswith(coretext.SHOWN)]
        self.assertGreaterEqual(len(keys), 5)
        en, rus = catalog("en"), catalog("ru")
        for key in keys:
            self.assertIn(key[:-len(coretext.SHOWN)], coretext.TEMPLATES, key)
            for text in (coretext.TEMPLATES[key], en[key], rus[key]):
                self.assertNotRegex(text, r"REPLIED|STOPPED|TIMEOUT|\{id\}|\{mid\}", (key, text))

    def test_a_sessions_hub_show_keeps_the_tag_and_the_numbers(self):
        # the row is one whoever folds it (the live feed, the page's archive parse): the label is the words the owner
        # reads; a session's own process (HUB_SESSION: `hub show`, `hub show --grep`) prints the text — "REPLIED d8: …
        # on m5" is what it acts on — and finds the row by the numbers, which only the text has (round three of PR #734)
        from unittest import mock
        text = delegations.REPLIED.format(id="d8", name="a", target=delegations.TO_OWNER.format(mid="m5"), short="x")
        msg = {"id": "m9", "text": str(text), "l10n": coretext.marks(text)}
        row = views.core_row(msg, 9, 109, self.st)
        self.assertEqual(row["label"], 'a replied to the owner: "x"')
        self.assertTrue(views.core_words(row))
        with mock.patch.dict(os.environ, {"HUB_SESSION": "hub"}):
            self.assertEqual(views.core_row(msg, 9, 109, self.st), row)   # the same row in a session's process
            line = views._item_text(row)
            self.assertIn("REPLIED d8: a replied to the owner on m5: \"x\"", line)
            self.assertNotIn('the owner: "x"', line.replace('the owner on m5: "x"', ""))
            # the archive's row (history.grep gives it to views.grep as older): found by the task number, printed
            # in the session's form
            self.assertEqual(views.found(row, "d8"), str(text))
            self.assertEqual(views.found(row, "m5"), str(text))
            self.assertEqual(list(views.search_texts(row)), [row["label"], str(text)])
            reader = views.Reader()
            hits = views.grep(reader, "d8", tz="UTC", older=[("hub", row)])
            self.assertEqual(len(hits), 1)
            self.assertIn("REPLIED d8: a replied to the owner on m5", hits[0])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HUB_SESSION", None)
            self.assertTrue(views._item_text(row).endswith(': a replied to the owner: "x"'))   # the owner's terminal
            self.assertEqual(views.found(row, "replied to the owner"), row["label"])   # the owner's words
            # the numbers lie only in the text: the owner's search does not find them (the page's own search and the
            # archive search agree: the first non-empty field, the label), a session's does (above)
            self.assertIsNone(views.found(row, "d8"))
            self.assertIsNone(views.found(row, "m5"))
            self.assertEqual(list(views.search_texts(row)), [row["label"]])
        # a card answer the core retells in the owner's name keeps its short label in a session's process too
        answer = views.core_row({"id": "m10", "text": "Answer to question q3: yes", "on_behalf": True,
                                 "answers_question": "q3", "answer": "yes"}, 10, 110, self.st)
        self.assertFalse(views.core_words(answer))

    def test_a_plain_phrase_takes_the_plain_target_in_french_spanish_and_portuguese(self):
        # the target's shown variant (au hub, a la sesión b, à sessão b) is for the phrases that have a variant of their
        # own; a reminder on a task and the refusal of a spec have none, and their words expect the plain target: "La
        # tâche d5 de la session b", not "de à la session b" (round three of PR #734)
        wrong = {"fr": r"\bde à la session\b|\bpar au hub\b", "es": r"\bde a la sesión\b|\ba al hub\b",
                 "pt-BR": r"\bde à sessão\b|\ba ao hub\b"}
        want = {"fr": "de la session b", "es": "de la sesión b", "pt-BR": "de sessão b"}
        for lang in wrong:
            for phrase in (delegations.NUDGE_TEXT, delegations.NUDGE_LAST):
                text = phrase.format(id="d5", who=delegations.SESSION_WORD.format(name="b"), mins=10)
                msg = {"id": "m9", "text": str(text), "l10n": coretext.marks(text)}
                out = coretext.shown(msg, lang)
                self.assertNotRegex(out, wrong[lang], (lang, out))
                self.assertIn(want[lang], out, (lang, out))
                self.assertIn("d5", out, (lang, out))   # no variant: the task number stays
            text = delegations.SPEC_WHO.format(id="d5", boss=delegations.TO_HUB)
            out = coretext.shown({"id": "m9", "text": str(text), "l10n": coretext.marks(text)}, lang)
            self.assertNotRegex(out, wrong[lang], (lang, out))
        # while the phrase with a variant keeps the contracted target of its own
        text = delegations.REPLIED.format(id="d8", name="a", target=delegations.TO_HUB, short="x")
        self.assertIn("au hub", coretext.shown({"id": "m9", "text": str(text), "l10n": coretext.marks(text)}, "fr"))

    def test_the_owners_line_contracts_the_preposition_in_french_spanish_and_portuguese(self):
        # "à le", "a al", "para ao" (round two of PR #734): the target carries the contracted preposition (au, al, ao)
        # and the phrase drops its own — the English variants of the targets exist for that; a session name likewise
        wrong = r"\bà le\b|\ba al\b|\bpara ao\b|\ba ao\b|\bà la\b.*\bà la\b"
        want = {"fr": ["au propriétaire", "au hub", "à la session b"],
                "es": ["al titular", "al hub", "a la sesión b"],
                "pt-BR": ["ao titular", "ao hub", "à sessão b"]}
        targets = [delegations.TO_OWNER.format(mid="m5"), delegations.TO_HUB, delegations.SESSION_WORD.format(name="b")]
        for lang, words in want.items():
            for phrase in (delegations.REPLIED, delegations.REPLIED_MORE):
                for target, word in zip(targets, words):
                    text = phrase.format(id="d8", name="a", target=target, short="x")
                    out = coretext.shown({"text": str(text), "l10n": coretext.marks(text)}, lang)
                    self.assertIn(word, out, (lang, out))
                    self.assertNotRegex(out, wrong, (lang, out))
                    self.assertNotRegex(out, r"\b[dm]\d+\b", (lang, out))


class Text(unittest.TestCase):
    def test_the_word_for_nothing_to_continue_is_the_one_the_core_recognizes(self):
        self.assertEqual(reducer.NOTHING, delegations.NOTHING)

    def test_a_head_put_before_the_text_moves_the_marks_with_it(self):
        # a queued core message moves to the raiser of a stopped executor with a head line; the hub relays an answer
        # to a card with one (reducer: coretext.prepend)
        msg = reducer.new_message({"messages": {}, "sessions": {}}, "m1", "x", "Words." + reducer.RELAYED_TAIL, 1)
        coretext.prepend(msg, "(Head.)\n")
        self.assertEqual(msg["text"], "(Head.)\nWords." + str(reducer.RELAYED_TAIL))
        self.assertEqual(coretext.shown(msg, "ru"), "(Head.)\nWords." + ru("core.msg.relayed_tail"))
        coretext.prepend(msg, board.HANDED_HEAD.format(name="e") + " ")
        self.assertEqual(coretext.shown(msg, "ru"), ru("core.msg.handed_head", name="e") + " (Head.)\nWords."
                         + ru("core.msg.relayed_tail"))
        plain = {"text": "plain"}
        coretext.prepend(plain, "head ")
        self.assertEqual(plain, {"text": "head plain"})
        odd = {"text": "Words." + str(reducer.RELAYED_TAIL), "l10n": [["bad"], [6, len(reducer.RELAYED_TAIL),
                                                                              "core.msg.relayed_tail", {}]]}
        coretext.prepend(odd, "(Head.)\n")   # one odd mark from an older record does not take the good ones with it
        self.assertEqual(coretext.shown(odd, "ru"), "(Head.)\nWords." + ru("core.msg.relayed_tail"))
        self.assertEqual(str(coretext.join("Executor ", 3, " done")), "Executor 3 done")

    def test_a_format_spec_is_refused_and_the_marks_share_nothing_with_the_text(self):
        # the catalogs substitute plain {name}: a spec would never render for the owner, so the phrase is refused at import
        for bad in ("longer than {hours:.1f} h", "{what!r}", {"one": "{n:d} item", "other": "{n} items"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                coretext.T("core.msg.test_only", bad)
        self.assertNotIn("core.msg.test_only", coretext.TEMPLATES)
        nested = executors._what({"ahead": 2})
        text = executors.UNMERGED.format(branch="b", base="main", what=nested)
        stored = coretext.marks(text)
        stored[0][3]["what"]["l10n"][0][3]["count"] = 9
        self.assertEqual(nested.spans[0][3]["count"], 2)
        self.assertEqual(text.spans[0][3]["what"]["l10n"][0][3]["count"], 2)

    def test_a_fixed_substitution_leaves_the_others_for_format(self):
        # a phrase with the recognised word fixed and a substitution of its own (the folder of a moved session)
        try:
            p = coretext.T("core.msg.test_only", "Moved to {cwd}; nothing — answer «{nothing}».", nothing="none")
            self.assertEqual(str(p), "Moved to {cwd}; nothing — answer «none».")
            text = p.format(cwd="/home/x/m")
            self.assertEqual(str(text), "Moved to /home/x/m; nothing — answer «none».")
            self.assertEqual(text.spans[0][3], {"nothing": "none", "cwd": "/home/x/m"})
        finally:
            coretext.TEMPLATES.pop("core.msg.test_only", None)

    def test_a_nested_built_argument_is_translated_too(self):
        what = executors._what({"ahead": 3, "dirty": True})
        self.assertEqual(str(what), " (3 commits ahead; uncommitted changes in the copy)")
        msg = reducer.new_message({"messages": {}, "sessions": {}}, "m1", "x",
                                  executors.UNMERGED.format(branch="task/x", base="main", what=what), 1)
        shown = coretext.shown(msg, "ru")
        self.assertEqual(shown, ru("core.msg.unmerged", branch="task/x", base="main", what=" (" + ru(
            "core.msg.unmerged_ahead", count=3) + "; " + ru("core.msg.unmerged_dirty") + ")"))
        self.assertNotEqual(ru("core.msg.unmerged_ahead", count=3)[1:], ru("core.msg.unmerged_ahead", count=1)[1:])
        self.assertNotIn("ahead", shown)
        one = executors._what({"ahead": 1})
        self.assertEqual(str(one), " (1 commit ahead)")
        msg = reducer.new_message({"messages": {}, "sessions": {}}, "m2", "x", executors.UNMERGED_UP.format(
            name="e", branch="b", base="main", what=one, again=executors.AGAIN), 1)
        self.assertEqual(coretext.shown(msg, "ru"), ru("core.msg.unmerged_up", name="e", branch="b", base="main", what=" ("
                                                         + ru("core.msg.unmerged_ahead", count=1) + ")",
                                                         again=ru("core.msg.unmerged_again")))

    def test_a_text_edited_after_it_was_made_keeps_that_span_in_english(self):
        msg = reducer.new_message({"messages": {}, "sessions": {}}, "m1", "x", reducer.CUT_TURN, 1)
        msg["text"] = "Prefix. " + msg["text"]   # a later rewrite of the message: the marks no longer fit
        self.assertIsNone(coretext.shown(msg, "ru"))

    def test_old_messages_and_odd_marks_are_shown_as_they_are(self):
        self.assertIsNone(coretext.shown({"text": "a plain old text"}, "ru"))
        for marks in ("x", [[0]], [["a", 1, "k", {}]], [[0, 3, "core.msg.no_such_key", {}]], [[0, 3, 5, None]]):
            with self.subTest(marks=marks):
                self.assertIsNone(coretext.shown({"text": "abc", "l10n": marks}, "ru"))

    def test_a_phrase_built_by_concatenation_keeps_both_marks(self):
        text = "Lead: " + reducer.LESSON_FORM + " / " + board.HANDED_HEAD.format(name="e")
        self.assertEqual([s[2] for s in text.spans], ["core.msg.lesson_form", "core.msg.handed_head"])
        msg = {"text": str(text), "l10n": coretext.marks(text)}
        shown = coretext.shown(msg, "ru")
        self.assertEqual(shown, "Lead: " + ru("core.msg.lesson_form") + " / " + ru("core.msg.handed_head", name="e"))



def refuse_with_a_phrase(state):
    """A refusal whose reason is a core phrase, written by the real path (daemon.Hub.rejection, reducer's Rejected)."""
    j = journal.Journal(os.path.join(state, "journal.jsonl"))
    hub = types.SimpleNamespace(journal=j, clock=time.time)
    why = board.HANDED_HEAD.format(name="e")   # a phrase stands in for a refusal C3 will write in English
    for path, ev in spool.take(os.path.join(state, "spool")):
        ev = j.event(ev["type"], ev.get("entity"), ev.get("data"), src="cli")
        j.write(ev, daemon.Hub.rejection(hub, ev, reducer.forms.Why(why, "test_code")))   # one write, as hubd's
        os.unlink(path)
        return True
    return False


class Refusals(unittest.TestCase):
    """C1: the reason of core.rejected carries the marks of the core's phrases; whoever shows it to the owner (the
    terminal, the page, Telegram) renders them in his language, a session reads the English."""

    def test_the_rejection_keeps_the_marks_and_decided_gives_them_back_with_the_code(self):
        state = tempfile.mkdtemp(prefix="coretext-")
        spool.put(os.path.join(state, "spool"), "message.received", None, {"to": "a", "text": "x", "req": "r1"}, src="cli")
        self.assertTrue(refuse_with_a_phrase(state))
        (rej,) = [e for e in journal.read(os.path.join(state, "journal.jsonl")) if e["type"] == "core.rejected"]
        self.assertEqual(rej["data"]["reason"], "Executor e handed in its task and fell asleep.")
        self.assertEqual([m[2] for m in rej["data"]["l10n"]], ["core.msg.handed_head"])
        _, why = journal.decided(os.path.join(state, "journal.jsonl"), "r1", 1)
        self.assertEqual((str(why), why.code), (rej["data"]["reason"], "test_code"))
        ruled = coretext.human(why, "ru")
        self.assertEqual((str(ruled), ruled.code), (ru("core.msg.handed_head", name="e"), "test_code"))
        self.assertEqual(str(coretext.human(why, "en")), rej["data"]["reason"])
        plain = coretext.why_of({"reason": "a plain old reason", "code": "x"})   # an earlier record: as it is
        self.assertEqual((coretext.human(plain, "ru"), plain.code), ("a plain old reason", "x"))

    def test_the_owners_terminal_reads_the_refusal_in_his_language_a_session_in_english(self):
        from tests.test_cli_edges import HUB
        state = tempfile.mkdtemp(prefix="coretext-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        for n in ("a", "b"):
            j.append("session.requested", n, {"cwd": state, "session_id": f"s-{n}", "channel": "cli", "owner": True})
            j.append("session.started", n, {"session_id": f"s-{n}"})
        cfg = os.path.join(state, "hub.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({}, f)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=state, HUB_CONFIG=cfg, HUB_CLI_WAIT="10", HUB_LANG="ru")
        for me, want, other in ((None, ru("core.msg.handed_head", name="e"), "Executor e handed in"),
                                ("a", "Executor e handed in its task and fell asleep.", ru("core.msg.handed_head")[:12])):
            with self.subTest(me=me):
                p = subprocess.Popen([sys.executable, HUB, "send", "b", "hello"], text=True, encoding="utf-8",
                                     env={**env, **({"HUB_SESSION": me} if me else {})},
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                end = time.time() + 20
                while not refuse_with_a_phrase(state) and time.time() < end:
                    time.sleep(0.05)
                out, _ = p.communicate(timeout=30)
                self.assertIn(want, out)
                self.assertNotIn(other, out)

    def test_the_page_names_a_refused_message_in_the_hubs_language(self):
        r = views.Reader()
        seq = 0
        for type_, entity, data in (("session.requested", "a", {"session_id": "s", "cwd": "/w"}),
                                    ("message.received", None, {"to": "a", "text": "hi", "channel": "web", "owner": True,
                                                                "client_id": "c1"})):
            seq += 1
            r.feed({"seq": seq, "ts": seq, "type": type_, "entity": entity, "data": data})
        why = board.HANDED_HEAD.format(name="e")
        r.feed({"seq": seq + 1, "ts": seq + 1, "type": "core.rejected", "entity": None,
                "data": {"of": seq, "type": "message.received", "reason": str(why), "l10n": coretext.marks(why)}})
        i18n.use("ru")   # a Russian hub
        try:
            self.assertEqual(r.refusal("a", "c1")["error"], ru("core.msg.handed_head", name="e"))
        finally:
            i18n.use("en")
        self.assertEqual(coretext.human(coretext.why_of({"reason": str(why), "l10n": coretext.marks(why)})), str(why))

    def test_a_page_command_refused_answers_in_the_requests_language(self):
        # decide() gives the reason as the core wrote it (codes and REFUSED_IN read it), said() in the response language
        from unittest import mock
        from modules.web import server
        phrase = board.HANDED_HEAD.format(name="e")
        why = coretext.why_of({"reason": str(phrase), "code": "c", "l10n": coretext.marks(phrase)})
        for lang, want in (("ru", ru("core.msg.handed_head", name="e")), ("en", str(why))):
            web = types.SimpleNamespace(state_dir=tempfile.mkdtemp(), decide_wait=0, put=lambda *a: "box",
                                        lang_now=lambda lang=lang: lang)
            with mock.patch.object(server.journal, "decided", lambda *a, **k: (7, why)):
                seq, got, box = server.Web.decide(web, "session.close_requested", "a", {})
            self.assertEqual((seq, str(got), box), (7, str(why), "box"))
            said = server.Web.said(web, got)
            self.assertEqual((str(said), said.code), (want, "c"))

    def test_a_bus_flow_says_the_reason_in_the_owners_language(self):
        from core import busflow
        phrase = board.HANDED_HEAD.format(name="e")
        why = coretext.why_of({"reason": str(phrase), "l10n": coretext.marks(phrase)})
        i18n.use("ru")   # a Russian owner
        try:
            self.assertIn(ru("core.msg.handed_head", name="e"), busflow.why("rejected", why))
        finally:
            i18n.use("en")
        self.assertIn(str(phrase), busflow.why("rejected", why, lang="en"))

    def test_the_bus_cli_says_a_refusal_in_the_owners_language(self):
        import contextlib
        import io
        from core import buscli
        phrase = board.HANDED_HEAD.format(name="e")
        why = coretext.why_of({"reason": str(phrase), "l10n": coretext.marks(phrase)})
        out = io.StringIO()
        i18n.use("ru")   # a Russian owner
        try:
            with contextlib.redirect_stdout(out):
                buscli.Cli.refused(None, "rejected", why)
        finally:
            i18n.use("en")
        self.assertIn(ru("core.msg.handed_head", name="e"), out.getvalue())

    def test_hub_show_names_the_cores_words_in_the_readers_language(self):
        row = {"kind": "in", "channel": "core", "sender": "core", "text": "Executor e handed in.", "label": "RU"}
        self.assertTrue(views._item_text(row).endswith(": RU"))
        relayed = {**row, "channel": "session", "sender": "hub"}   # the hub's words with the core's tail
        self.assertTrue(views._item_text(relayed).endswith(": RU"))
        mirror = {**row, "channel": "session", "sender": None, "to": "e"}   # words to an executor in the hub's feed
        self.assertTrue(views._item_text(mirror).endswith(": RU"))
        own = {**row, "channel": "web", "sender": None}   # the owner's own words, a button press label aside
        self.assertTrue(views._item_text(own).endswith(": Executor e handed in."))

    def test_a_late_refusal_to_a_session_keeps_the_phrase(self):
        # the CLI did not wait: the core tells the session by a message, the reason's phrase goes with it
        st = reducer.empty()
        for i, (t, e, d) in enumerate((("session.requested", "a", {"cwd": "/w", "session_id": "s", "channel": "cli",
                                                                    "owner": True}),
                                       ("session.started", "a", {}))):
            st, _, _ = reducer.reduce(st, {"seq": i + 1, "ts": i + 1, "type": t, "entity": e, "data": d})
        why = board.HANDED_HEAD.format(name="e")
        for i, (t, d) in enumerate((("core.rejected", {"of": 9, "type": "x.y", "reason": str(why), "req": "r",
                                                       "sender": "a", "l10n": coretext.marks(why)}),
                                    ("cli.gave_up", {"req": "r", "what": "x.y", "channel": "session", "owner": False,
                                                     "sender": "a"}))):
            st, _, _ = reducer.reduce(st, {"seq": 10 + i, "ts": 10 + i, "type": t, "entity": None, "data": d})
        (msg,) = [m for m in st["messages"].values() if m["session"] == "a"]
        self.assertIn(str(why), msg["text"])
        self.assertIn(ru("core.msg.handed_head", name="e"), coretext.shown(msg, "ru"))


if __name__ == "__main__":
    unittest.main()
