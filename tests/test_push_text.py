"""Push text (push spec, 2.7): one function owner.push_text for all reasons. Title <= 40 is the topic: for a question the
form's `header` (except generic labels), for a notice "<sender>: <topic|title>"; otherwise the fallback is the first
meaningful sentence. Text <= 160: for a notice its `text`, for a question the first sentence and the sentence with "?".
Cleaning removes markup, "Summary:" and "TL;DR" labels, ids, leading emoji; if empty, "<session> waits for an answer".
Truncation is at a word, and "..." counts toward the limit. Urgency comes only from the notice author. The session name
is the second line (subtitle). The client acceptance scenarios are those compatible with the owner's decisions (no
urgency from text and no session name)."""
import tests  # noqa: F401 — hide the machine's settings
import re
import unittest

from core import langdata, owner, reducer

DAY = 1791979200   # 2026-10-14 12:00 UTC: daytime, so the reminder is not moved to the morning


def form(*questions):
    """The AskUserQuestion form: (question, header) per question."""
    return {"request_id": "r1", "tool": "AskUserQuestion", "since": DAY,
            "input": {"questions": [{"question": q, **({"header": h} if h is not None else {})} for q, h in questions]}}


def text_of(req, session="maps", v=0, now=DAY + 100):
    st = reducer.empty()
    st["sessions"][session] = {"name": session, "state": "idle", "since": 1, "pending": [req]}
    if v:
        st["owner"]["reminders"][f"q:{session}:r1"] = v
    (a,) = owner.alerts(st, now)
    return a


def clean(a):
    """Common client checks: no labels, ids, markup; title <= 40, text <= 160, not empty."""
    for field, limit in (("title", owner.PUSH_TITLE), ("body", owner.PUSH_BODY)):
        value = a[field]
        assert value.strip(), (field, a)
        assert len(value) <= limit, (field, len(value), value)
        assert not re.match(r"(?i)\s*(summary|tl;dr|" + langdata.alt("words", "topic_label") + r")\b", value), (field, value)
        assert not re.search(r"\*\*|`|\[hub:|\bq\d{3,}\b", value), (field, value)
    return a


class Questions(unittest.TestCase):
    def test_the_header_is_the_topic_and_the_session_is_the_subtitle(self):
        a = clean(text_of(form(("The spec of maps-1 is ready. Do you approve the spec?", "Spec"))))
        self.assertEqual((a["title"], a["subtitle"]), ("Spec", "maps"))
        self.assertEqual(a["body"], "The spec of maps-1 is ready. Do you approve the spec?")

    def test_a_long_question_keeps_the_first_sentence_and_the_question(self):
        q = ("The router password will fix nothing: I may not enter passwords. Changing the DNS servers to 1.1.1.1 "
             "and 8.8.8.8 in the router itself will fix it: then the service works on all devices and the check passes by itself. "
             "What do we do?")
        a = clean(text_of(form((q, "DNS"))))
        self.assertEqual(a["body"], "The router password will fix nothing: I may not enter passwords. What do we do?")

    def test_too_long_together_takes_the_question_cut_by_a_word(self):
        q = "The first sentence is quite long, " * 4 + "the end. " + "Do we take the option " * 12 + "or not?"
        a = clean(text_of(form((q, "Choice of option"))))
        self.assertTrue(a["body"].endswith("…"))
        self.assertTrue(a["body"].startswith("Do we take the option"))
        cut = a["body"][:-1]
        self.assertTrue(q[q.index(cut) + len(cut)] == " ")                          # not mid-word

    def test_a_generic_or_label_header_falls_back_to_the_first_sentence(self):
        generic = langdata.words("words", "topic_generic")   # the Russian generic headers (language data)
        for header in (generic[2].capitalize(), "Summary:", "", None, generic[0].capitalize()):
            a = clean(text_of(form(("Please look at the feed layout option for the phone.", header))))
            self.assertEqual(a["title"], "Please look at the feed layout option…", header)

    def test_a_summary_label_is_not_shown(self):
        # client acceptance, scenario 1: a long card with a "Summary:" label: no label, topic <= 40, the question in the
        # text
        q = ("Summary: a basket of 11 items. Shop A — 830 999 and delivery 47 228, shop B — 818 885, arrives on "
             "Wednesday 28.10. Which basket to place?")
        a = clean(text_of(form((q, "Summary"))))
        self.assertEqual(a["title"], "A basket of 11 items")
        self.assertEqual(a["body"], "A basket of 11 items. Which basket to place?")
        # scenario 7: starts with "Summary:", no topic and no deadline
        a = clean(text_of(form(("**Summary:** Please look at the feed layout option.", None))))
        self.assertEqual(a["body"], "Please look at the feed layout option.")

    def test_markup_ids_and_emoji_are_cleaned(self):
        a = clean(text_of(form(("🚀 **Rolling out** `v2` today? q512 d634 [hub:m5]", "✅ Rollout"))))
        self.assertEqual(a["title"], "Rollout")
        self.assertEqual(a["body"], "Rolling out v2 today?")

    def test_the_meaning_stays(self):
        # cleaning leaves alone a number's minus, $, underscores in names, ">" in text and short markers like d3
        for q in ("-5 degrees outside. Ok?", "$100 or 200?", "Rename file_name if a > b?",
                  "Update d3 and m2 to q4?"):
            self.assertEqual(text_of(form((q, "Topic")))["body"], q)

    def test_more_leading_symbols_are_cleaned(self):
        for mark in ("⏰", "⏸", "→", "▶", "‼"):
            self.assertEqual(text_of(form((f"{mark} Deadline today. Rolling out?", None)))["title"], "Deadline today", mark)

    def test_only_labels_give_the_session_waits(self):
        a = clean(text_of(form(("Summary:", "Summary"))))
        self.assertEqual((a["title"], a["body"]), ("maps is waiting for your answer", "maps is waiting for your answer"))

    def test_a_word_longer_than_the_limit_is_cut_by_char_with_the_ellipsis_inside(self):
        a = clean(text_of(form(("Y" * 300, None))))
        self.assertEqual(len(a["title"]), owner.PUSH_TITLE)
        self.assertTrue(a["title"].endswith("…"))
        self.assertEqual(len(a["body"]), owner.PUSH_BODY)

    def test_a_form_of_several_questions_uses_the_first_and_counts_the_rest(self):
        a = clean(text_of(form(("Publish the issue?", "Issue"), ("Write the post?", "Post"), ("To whom?", "Addressee"))))
        self.assertEqual((a["title"], a["body"]), ("Issue", "Publish the issue? (+2)"))

    def test_a_permission_without_text(self):
        req = {"request_id": "r1", "tool": "Bash", "since": DAY, "input": {"command": "git push origin main --force"}}
        a = clean(text_of(req))
        self.assertEqual((a["title"], a["body"]), ("Allow Bash?", "git push origin main --force"))

    def test_a_reminder_keeps_the_topic_and_says_how_long_it_waits(self):
        # client acceptance, scenario 4: a reminder has the same title (without the word "Reminder" in it)
        first = text_of(form(("Confirm the guest check-in?", "Guest check-in")))
        again = clean(text_of(form(("Confirm the guest check-in?", "Guest check-in")), v=1))
        self.assertEqual(again["title"], first["title"])
        self.assertEqual(again["body"], "Waiting 30 min: Confirm the guest check-in?")
        self.assertEqual(text_of(form(("Confirm?", "Check-in")), v=2)["body"], "Waiting 3 h: Confirm?")

    def test_no_urgency_is_made_up_from_the_text(self):
        # client acceptance, scenario 2, by the owner's decision: urgency only from the author, none from the question
        # text
        a = clean(text_of(form(("The guest arrives tomorrow at 10:00. Without an answer by 18:00 the booking is dropped. "
                                "Confirm?", None))))
        self.assertEqual(a["title"], "The guest arrives tomorrow at 10:00")
        self.assertEqual(a["body"], "The guest arrives tomorrow at 10:00. Confirm?")
        self.assertNotRegex(a["body"], "^(Now|Can wait)")


class Tools(unittest.TestCase):
    def test_a_tool_request(self):
        st = reducer.empty()
        st["sessions"]["maps"] = {"name": "maps", "state": "idle", "since": 1, "requests": [
            {"tool": "browser:lobo", "state": "needs-owner", "since": 1000, "why": "check the login"}]}
        (a,) = owner.alerts(st, 1100)
        self.assertEqual((a["title"], a["subtitle"], a["body"]), ("Connect browser:lobo?", "maps", "check the login"))


class Notices(unittest.TestCase):
    def notice(self, data, src="module"):
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1000, "type": "owner.notify", "entity": None,
                                                      "src": src, "data": data})
        self.assertIsNone(why)
        (a,) = owner.alerts(st, 1100)
        return clean(a)

    MOD = {"channel": "plugin", "owner": False, "sender": "mod:shop"}

    def test_the_sender_leads_the_title_and_the_author_text_is_the_body(self):
        a = self.notice({"text": "Checked three clients: the panel icons are fixed. TL;DR: all ok.", "topic": "Icons",
                         **self.MOD})
        self.assertEqual(a["title"], "shop: Icons")
        self.assertEqual(a["body"], "Checked three clients: the panel icons are fixed. All ok.")
        self.assertNotIn("subtitle", a)

    def test_urgency_and_due_come_only_from_the_author(self):
        a = self.notice({"text": "Without a login the order will not go.", "topic": "Login to the delivery service", "urgency": "now",
                         "due": "16:00", **self.MOD})
        self.assertEqual(a["body"], "Now: by 16:00. Without a login the order will not go.")
        a = self.notice({"text": "The estimate is valid until Friday.", "urgency": "later", **self.MOD})
        self.assertEqual((a["title"], a["body"]), ("shop", "Can wait. The estimate is valid until Friday."))

    def test_an_empty_title_and_a_broken_due_fall_back(self):
        a = self.notice({"text": "**", "channel": "core", "owner": False}, src="core")
        self.assertEqual(a["title"], "core")
        from core import owner as o
        n = {"id": "n:1", "sender": "mod:shop", "since": 1, "at": 1, "v": 1, "title": "", "text": "x", "urgent": False,
             "urgency": "now", "due": 5e18}
        self.assertTrue(o.push_text({"kind": "notice", "notice": n})["body"].startswith("Now. "))

    def test_a_bad_urgency_is_refused(self):
        _, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1000, "type": "owner.notify", "entity": None,
                                                     "src": "module", "data": {"text": "x", "urgency": "asap", **self.MOD}})
        self.assertIsNotNone(why)

    def test_the_owner_without_a_title_gets_the_first_sentence_not_a_label(self):
        a = self.notice({"text": "The backup is ready. Space is low.", "channel": "cli", "owner": True}, src="cli")
        self.assertEqual(a["title"], "The backup is ready")

    def test_a_plugin_process_is_named(self):
        a = self.notice({"text": "x", "title": "Build", "channel": "plugin", "owner": False, "sender": "core"}, src="cli")
        self.assertEqual(a["title"], "core: Build")              # a plugin named "core" still gets the prefix

    def test_a_schedule_job_and_the_core(self):
        a = self.notice({"text": "the gate is closed", "title": "Shop", "channel": "cli", "owner": True, "source": "gate"},
                        src="cli")
        self.assertEqual(a["title"], "gate: Shop")
        a = self.notice({"text": "the file is unreadable", "title": "hub.json", "channel": "core", "owner": False,
                         "source": "core"}, src="core")
        self.assertEqual(a["title"], "hub.json")


if __name__ == "__main__":
    unittest.main()
