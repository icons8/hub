import unittest

from core import i18n, reducer, views
from tests.fakes import lang


def events(raw):
    return [{"seq": i, "ts": r.get("t", i), "src": r.get("src", "test"), "type": r["type"], "entity": r.get("entity"),
             "data": r.get("data", {})} for i, r in enumerate(raw, 1)]


class TaskNotificationTest(unittest.TestCase):
    def test_background_done_is_a_system_line(self):
        # a "Terminal input <task-notification>…" message is a service line, not input
        r = views.Reader()
        for ev in events([
            {"type": "session.requested", "entity": "a", "data": {"session_id": "s", "cwd": "/w"}},
            {"type": "turn.started", "entity": "a", "data": {"marker": None, "prompt_head": "<task-notification>\n<task-id>t1",
                                                             "notice": {"status": "completed", "summary": "Agent \"x\" completed"}}},
            {"type": "turn.started", "entity": "a", "data": {"marker": None, "prompt_head": "<task-notification>\n<task-id>t2"}},
            {"type": "turn.started", "entity": "a", "data": {"marker": None, "prompt_head": "by hand"}},
        ]):
            r.feed(ev)
        tl = r.timelines["a"]   # the whole core feed: the first line is "session created" (the page does not get it)
        self.assertEqual([i["kind"] for i in tl], ["sys", "sys", "sys", "terminal"])
        self.assertEqual(tl[1]["text"], 'background task finished: Agent "x" completed')
        self.assertEqual(tl[2]["text"], "background task finished")


class AutoAllowLine(unittest.TestCase):
    def test_a_request_the_core_allowed_is_a_system_line(self):
        # permissions.auto_allow (2026-10-14): the core allowed it itself — a feed line, not a question card
        r = views.Reader()
        for ev in events([
            {"type": "session.requested", "entity": "a", "data": {"session_id": "s", "cwd": "/w"}},
            {"type": "permission.requested", "entity": "a", "src": "host", "data": {
                "request_id": "r1", "tool": "Bash", "input": {"command": "cd /w/x; make > l"}, "auto": "allow",
                "why": "Compound command contains cd with output redirection"}},
            {"type": "permission.requested", "entity": "a", "src": "host", "data": {
                "request_id": "r2", "tool": "Edit", "input": {"file_path": "/w/.git/config"}, "auto": "allow"}},
        ]):
            r.feed(ev)
        tl = r.snapshot()["timelines"]["a"]
        self.assertNotIn("ask", [i["kind"] for i in tl])
        self.assertEqual([(i["kind"], i["text"]) for i in tl[-2:]], [
            ("sys", "the core allowed it itself: Bash cd /w/x; make > l (Compound command contains cd with output redirection)"),
            ("sys", "the core allowed it itself: Edit /w/.git/config")])

class TimelineTest(unittest.TestCase):
    def test_timeline_and_labels(self):
        r = views.Reader()
        for ev in events([
            {"type": "session.requested", "entity": "a", "data": {"session_id": "s", "cwd": "/w"}},
            {"type": "session.started", "entity": "a", "data": {"session_id": "s"}},
            {"type": "message.received", "data": {"to": "a", "text": "hello", "channel": "web"}},
            {"type": "turn.started", "entity": "a", "data": {"marker": "m3"}},
            {"type": "permission.requested", "entity": "a", "data": {"request_id": "r1", "tool": "AskUserQuestion",
                                                                     "input": {"questions": []}}},
            {"type": "turn.started", "entity": "zz", "data": {}},  # rejected by the core — does not reach the feed
            {"type": "permission.answered", "entity": "a", "data": {"request_id": "r1", "behavior": "allow",
                                                                    "channel": "web", "owner": True}},
            {"type": "session.said", "entity": "a", "data": {"text": "done"}},
            {"type": "message.received", "data": {"to": "a", "text": "from the hub", "channel": "session", "sender": "hub"}},
            {"type": "session.take_requested", "entity": "a"},
            {"type": "turn.started", "entity": "a", "data": {"marker": None, "prompt_head": "by hand"}},
        ]):
            r.feed(ev)
        snap = r.snapshot()
        # "session created" is a service line for developers: it is in the core feed, the page does not get it
        self.assertEqual(r.timelines["a"][0]["text"], "session created")
        self.assertEqual([i["kind"] for i in snap["timelines"]["a"]],
                         ["in", "ask", "answer", "out", "in", "sys", "terminal"])
        self.assertEqual(snap["timelines"]["a"][4]["sender"], "hub")
        self.assertIsNone(snap["timelines"]["a"][0]["sender"])
        self.assertEqual(snap["timelines"]["a"][0]["channel"], "web")
        self.assertEqual(snap["sessions"][0]["label"], "manual mode")
        self.assertEqual(snap["messages"]["m3"]["label"], "delivered")
        # delivery time: the page puts a message where the session took it, and a queued one at the bottom
        self.assertEqual(snap["messages"]["m3"]["delivered_at"], 4)
        self.assertNotIn("zz", snap["timelines"])


class RemovedSessionTimeline(unittest.TestCase):
    def feed_until_removed(self, r):
        for ev in events([
            {"type": "session.requested", "entity": "devops", "data": {"session_id": "s0", "cwd": "/w",
                                                                         "role": "agent"}},
            {"type": "session.started", "entity": "devops", "data": {"session_id": "s0"}},
            {"type": "session.requested", "entity": "devops-2", "data": {"session_id": "s1", "cwd": "/w",
                                                                           "executor": True, "raised_by": "devops",
                                                                           "channel": "cli", "owner": True}},
            {"type": "session.started", "entity": "devops-2", "data": {"session_id": "s1"}},
            {"type": "message.received", "data": {"to": "devops-2", "text": "tell me", "channel": "web",
                                                   "owner": True}},
            {"type": "turn.started", "entity": "devops-2", "data": {"marker": "m5"}},
            {"type": "session.said", "entity": "devops-2", "data": {"text": "details"}},
            {"type": "turn.stopped", "entity": "devops-2", "data": {}},    # the turn ended — free, can be closed
            {"type": "session.close_requested", "entity": "devops-2", "data": {"retire": True}},  # executor_done
            {"type": "session.remove_requested", "entity": "devops-2", "data": {"retired": True}},
        ]):
            r.feed(ev)
        return r

    def test_revive_of_the_same_conversation_keeps_the_feed(self):
        # PR #7: the departure of the one that handed over put it to sleep and deleted it, revive raised the same
        # conversation (claude --resume), but the page feed showed only "session restarted" — earlier lines were lost
        r = self.feed_until_removed(views.Reader())
        self.assertEqual(len(r.timelines["devops-2"]), 5)  # created, message, reply, close, removal — before the removal
        r.feed({"seq": 10, "ts": 10, "src": "test", "type": "session.requested", "entity": "devops-2",
               "data": {"session_id": "s1", "cwd": "/w", "revive": True}})
        self.assertEqual(len(r.timelines["devops-2"]), 6)  # revive of the same conversation — the feed stayed, plus a restart mark

    def test_a_brand_new_session_reusing_the_freed_name_gets_a_fresh_feed(self):
        # the same name but really a different session (not a revive, another session_id) — the old feed is not hers
        r = self.feed_until_removed(views.Reader())
        r.feed({"seq": 10, "ts": 10, "src": "test", "type": "session.requested", "entity": "devops-2",
               "data": {"session_id": "s2", "cwd": "/other"}})
        self.assertEqual(len(r.timelines.get("devops-2", [])), 1)  # only the new "session created" mark, no earlier ones


class ToolsStopWithProcess(unittest.TestCase):
    def test_running_step_stops_when_process_leaves(self):
        """A background agent step arrived and the session process was put to sleep — no reply will come, the step
        must not keep spinning."""
        r = views.Reader()
        for ev in events([
            {"type": "session.requested", "entity": "a", "data": {"session_id": "s", "cwd": "/w"}},
            {"type": "session.started", "entity": "a", "data": {"session_id": "s"}},
            {"type": "turn.tool", "entity": "a", "data": {"id": "t1", "name": "Bash", "input": {"command": "mocha"}}},
            {"type": "session.exited", "entity": "a", "data": {"why": "released"}},
            {"type": "session.requested", "entity": "b", "data": {"session_id": "s2", "cwd": "/w"}},
            {"type": "session.started", "entity": "b", "data": {"session_id": "s2"}},
            {"type": "session.close_requested", "entity": "b", "data": {"channel": "web", "owner": True}},
            {"type": "session.said", "entity": "b", "data": {"text": "HANDOFF-DONE\nDone: everything"}},
            {"type": "turn.result", "entity": "b", "data": {"text": "HANDOFF-DONE\nDone: everything"}},
            {"type": "turn.stopped", "entity": "b"},
            # after the process stopped, the tail of its output arrived: a background agent step (a real case — 06:53:15)
            {"type": "turn.tool", "entity": "b", "data": {"id": "t2", "name": "Bash", "input": {"command": "mocha"}}},
        ]):
            r.feed(ev)
        (tool,) = [i for i in r.snapshot()["timelines"]["a"] if i["kind"] == "tool"]
        self.assertEqual(tool["status"], "stopped")
        self.assertEqual(r.state["sessions"]["b"]["state"], "asleep")
        (tool,) = [i for i in r.snapshot()["timelines"]["b"] if i["kind"] == "tool"]
        self.assertEqual(tool["status"], "stopped")


class HandoffDoneMark(unittest.TestCase):
    """The owner saw "HANDOFF-DONE" in the feed and in the session list as if it were words for him: the mark is for the
    core (it puts the session to sleep), the owner sees only the text after it."""

    def feed(self, raw):
        r = views.Reader()
        for ev in events(raw):
            r.feed(ev)
        return r

    def test_a_handover_result_shows_without_the_mark(self):
        r = self.feed([
            {"type": "session.requested", "entity": "b", "data": {"session_id": "s", "cwd": "/w"}},
            {"type": "session.started", "entity": "b", "data": {"session_id": "s"}},
            {"type": "session.close_requested", "entity": "b", "data": {"channel": "web", "owner": True}},
            {"type": "session.said", "entity": "b", "data": {"text": "HANDOFF-DONE\nDone: the report is ready"}},
            {"type": "turn.result", "entity": "b", "data": {"text": "HANDOFF-DONE\nDone: the report is ready"}},
            {"type": "turn.stopped", "entity": "b"},
        ])
        self.assertEqual(r.state["sessions"]["b"]["state"], "asleep")   # the core still reads the mark
        rows = r.snapshot()["timelines"]["b"]
        said = [i for i in rows if i["kind"] in ("out", "reply", "sys")]   # the request itself is the hub's row, not hers
        self.assertFalse([i for i in said if "HANDOFF-DONE" in (i.get("text") or "")])
        self.assertIn("Done: the report is ready", [i["text"] for i in rows if i["kind"] == "out"])
        self.assertIn(i18n.t("core.views.handoff_asleep"), [i["text"] for i in rows if i["kind"] == "sys"])

    def test_a_colon_or_a_dash_after_the_mark_is_not_shown_nor_told_to_the_hub(self):
        for sep in (": ", " \u2014 ", " \u2013 ", " - "):
            text = "HANDOFF-DONE" + sep + "Done: the report is ready"
            r = self.feed([
                {"type": "session.requested", "entity": "hub", "data": {"session_id": "h", "cwd": "/w", "kind": "hub"}},
                {"type": "session.started", "entity": "hub", "data": {"session_id": "h"}},
                {"type": "session.requested", "entity": "b", "data": {"session_id": "s", "cwd": "/w"}},
                {"type": "session.started", "entity": "b", "data": {"session_id": "s"}},
                {"type": "session.close_requested", "entity": "b", "data": {"channel": "web", "owner": True}},
                {"type": "session.said", "entity": "b", "data": {"text": text}},
                {"type": "turn.result", "entity": "b", "data": {"text": text}},
                {"type": "turn.stopped", "entity": "b"},
            ])
            self.assertEqual(r.state["sessions"]["b"]["state"], "asleep", sep)
            self.assertIn("Done: the report is ready", [i["text"] for i in r.snapshot()["timelines"]["b"]
                                                        if i["kind"] == "out"], sep)
            (told,) = [m["text"] for m in r.state["messages"].values() if m["session"] == "hub"]
            self.assertTrue(told.endswith(":\nDone: the report is ready"), (sep, told))

    def test_the_text_after_the_mark_keeps_its_own_signs(self):
        for text, shown in (("HANDOFF-DONE\n- Done: a list", "- Done: a list"), ("HANDOFF-DONE -1 test red", "-1 test red"),
                            ("HANDOFF-DONE: .env is set", ".env is set"), ("said HANDOFF-DONE here", "said HANDOFF-DONE here")):
            self.assertEqual(reducer.shown_text(text), shown)

    def test_a_mark_alone_leaves_no_empty_row(self):
        r = self.feed([
            {"type": "session.requested", "entity": "b", "data": {"session_id": "s", "cwd": "/w"}},
            {"type": "session.started", "entity": "b", "data": {"session_id": "s"}},
            {"type": "session.said", "entity": "b", "data": {"text": "HANDOFF-DONE"}},
        ])
        self.assertFalse([i for i in r.snapshot()["timelines"]["b"] if i["kind"] == "out"])

    def test_the_session_list_line_has_no_mark(self):
        r = self.feed([
            {"type": "session.requested", "entity": "b", "data": {"session_id": "s", "cwd": "/w"}},
            {"type": "session.started", "entity": "b", "data": {"session_id": "s"}},
            {"type": "message.received", "data": {"text": "hand over", "channel": "web", "owner": True, "to": "b"}},
            {"type": "turn.started", "entity": "b", "data": {"marker": "m3"}},
            {"type": "turn.result", "entity": "b", "data": {
                "text": "HANDOFF-DONE Done: moved the report to the shared folder and closed the task\n"
                        "Open: nothing\nHanded over: to no one, the session had no open work\n"
                        "Background: none, no schedules left, background commands stopped"}},
        ])
        sess = r.state["sessions"]["b"]
        self.assertTrue(sess["out_head"].startswith("Done: moved the report"), sess["out_head"])
        self.assertTrue(sess["gist"]["text"].startswith("Done: moved the report"), sess["gist"])


class CoreMessagesInTimeline(unittest.TestCase):
    def test_messages_made_by_core_show_up_for_their_session(self):
        """2026-09-30: HANDOFF-REQUEST, "Connected" and verdicts to the hub are created by the core — without a feed
        line, a turn started by them merged with the previous one. A delegation is a task with the owner's words."""
        r = views.Reader()
        evs = [("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"}),
               ("session.started", "hub", {"session_id": "h"}),
               ("session.requested", "w", {"cwd": "/w", "session_id": "s"}),
               ("session.started", "w", {"session_id": "s"}),
               ("message.received", None, {"text": "check the mail", "channel": "web", "owner": True}),
               ("delegation.created", None, {"session": "w", "for": "m5", "context": "Gmail", "channel": "session",
                                             "owner": False, "sender": "hub"}),
               ("turn.started", "w", {"marker": "m6"}),
               ("turn.result", "w", {"text": "no mail"}),
               ("turn.stopped", "w", {}),
               ("delegation.done", "d6", {"channel": "session", "owner": False, "sender": "hub"})]
        for i, (t, e, d) in enumerate(evs, 1):
            r.feed({"seq": i, "ts": 100.0 + i, "src": "t", "type": t, "entity": e, "data": d})
        line = r.timelines["w"]
        task = next(x for x in line if x.get("task"))
        self.assertEqual((task["words"], task["context"], task["sender"]), ("check the mail", "Gmail", "hub"))
        # API 1.26: every in line has from
        self.assertEqual([x.get("from") for x in r.timelines["hub"] + line if x["kind"] == "in"],
                         [{"kind": "owner"}, {"kind": "core"}, {"kind": "session", "name": "hub"}, {"kind": "hub"}])
        # "Done" on a delegation — the core itself asks to hand over: this core message is visible in the session feed
        handoff = [x for x in line if x["kind"] == "in" and x["text"].startswith("HANDOFF-REQUEST")]
        self.assertEqual([(x["sender"], x["channel"]) for x in handoff], [("hub", "core")])
        self.assertEqual(handoff[0]["from"], {"kind": "hub"})   # API 1.26: the sender as a field
        self.assertEqual(views._who({"origin": lang.origin("core")}), ":core")   # a label of the earlier Russian texts
        self.assertEqual(views._who({"origin": f" {lang.legacy('from')} mail-check, a reply to task d1"}), "mail-check")
        self.assertEqual(views._who({"origin": " from mail-check, reply to task d1"}), "mail-check")


class TaskBriefSpan(unittest.TestCase):
    """The setter's words of a task and the core's frame around them: the core says where the words lie (`brief`), the
    page does not guess it from the text."""

    def row(self, context, lead, words_of=None):
        r = views.Reader()
        evs = [("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"}),
               ("session.started", "hub", {"session_id": "h"}),
               ("session.requested", "w", {"cwd": "/w", "session_id": "s"}),
               ("session.started", "w", {"session_id": "s"}),
               ("delegation.created", None, {"session": "w", "for_session": "hub", "context": context, "channel": "session",
                                             "owner": False, "sender": "hub", **({"lead": lead} if lead is not None else {})})]
        for i, (t, e, d) in enumerate(evs, 1):
            r.feed({"seq": i, "ts": 100.0 + i, "src": "t", "type": t, "entity": e, "data": d})
        return next(x for x in r.timelines["w"] if x.get("task"))

    def test_span_covers_the_brief_in_utf16_units(self):
        brief = "fix the 😀 page"
        row = self.row(brief + "\n\nYou are the executor of this task.", len(brief))
        units = lambda s: len(s.encode("utf-16-le")) // 2
        a, b = row["brief"]
        text = row["text"]
        self.assertEqual(text.encode("utf-16-le")[a * 2:b * 2].decode("utf-16-le"), brief)
        self.assertTrue(text.startswith("Task from session hub."))
        self.assertEqual(units(text[:text.index(brief)]), a)

    def test_a_short_brief_is_found_in_the_context_not_in_the_head(self):
        row = self.row("Task", 4)   # the head is "Task from session hub." — the same word
        a, b = row["brief"]
        self.assertEqual(row["text"][a:b], "Task")
        self.assertEqual(a, len("Task from session hub.\n"))

    def test_the_owners_quote_repeating_the_brief_is_not_taken_for_it(self):
        r = views.Reader()
        for i, (t, e, d) in enumerate([
                ("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"}),
                ("session.started", "hub", {"session_id": "h"}),
                ("session.requested", "w", {"cwd": "/w", "session_id": "s"}),
                ("session.started", "w", {"session_id": "s"}),
                ("message.received", None, {"text": "check the mail", "channel": "web", "owner": True}),
                ("delegation.created", None, {"session": "w", "for": "m5", "context": "check the mail", "lead": 14,
                                              "channel": "session", "owner": False, "sender": "hub"})], 1):
            r.feed({"seq": i, "ts": 100.0 + i, "src": "t", "type": t, "entity": e, "data": d})
        task = next(x for x in r.timelines["w"] if x.get("task"))
        a, b = task["brief"]
        self.assertEqual(task["text"][a:b], "check the mail")
        self.assertEqual(task["text"].count("check the mail"), 2)
        self.assertGreater(a, task["text"].index("check the mail"))   # the second one, the context's

    def test_old_task_has_no_span(self):
        self.assertNotIn("brief", self.row("fix the page", None))

    def test_wrong_lead_is_no_span(self):
        for lead in (0, -1, 999, True, "5", 1.5):
            self.assertNotIn("brief", self.row("fix the page", lead), lead)


class FailedTurnIsNotAnAnswer(unittest.TestCase):
    def test_limit_text_is_one_error_line_not_the_sessions_answer(self):
        """2026-10-07: "You've hit your session limit · resets 6am" — the host writes it both as a reply
        (session.said) and as a result with is_error; on the page the reply became the session's answer ("Brief"
        takes the last non-error). The shape is as in the journal: subtype success, is_error true."""
        r = views.Reader()
        text = "You've hit your session limit · resets 6am (Europe/Paris)"
        for ev in events([
            {"type": "session.requested", "entity": "a", "data": {"session_id": "s", "cwd": "/w"}},
            {"type": "session.started", "entity": "a", "data": {"session_id": "s"}},
            {"type": "message.received", "data": {"to": "a", "text": "do it", "channel": "web", "owner": True}},
            {"type": "turn.started", "entity": "a", "data": {"marker": "m3"}},
            {"type": "session.said", "entity": "a", "data": {"text": "Looking at the code"}},
            {"type": "session.said", "entity": "a", "data": {"text": text}},
            {"type": "turn.result", "entity": "a", "data": {"text": text, "subtype": "success", "is_error": True,
                                                            "duration_ms": 796, "turns": 1, "tokens_in": 0}},
        ]):
            r.feed(ev)
        outs = [i for i in r.snapshot()["timelines"]["a"] if i["kind"] == "out"]
        self.assertEqual([(i["text"], bool(i.get("error"))) for i in outs], [("Looking at the code", False), (text, True)])



class ReplyMirror(unittest.TestCase):
    def test_reply_redirected_to_the_feed_of_the_owners_word_is_one_line(self):
        """A reply to a delegation (reply_to: dN) goes into the addressee's feed and, as a mirror, to where the
        owner's word was. A letter to a sleeping executor is redirected by the core to whoever woke it (the hub), and
        the owner's word is in the hub feed too: there were two identical in lines. One feed — one line."""
        st = {"sessions": {"hub": {"kind": "hub"}, "w": {}},
              "messages": {"m3": {"id": "m3", "session": "hub", "text": "check the mail"},
                           "m10": {"id": "m10", "session": "hub", "text": "done"}},
              "delegations": {"d5": {"for": "m3", "session": "w"}}}
        ev = {"seq": 10, "ts": 1.0, "type": "message.received", "src": "cli",
              "data": {"to": "w", "text": "done", "reply_to": "d5", "channel": "session", "sender": "maps"}}
        self.assertEqual([(who, it["kind"], it["id"]) for who, it in views._items(st, ev)], [("hub", "in", "m10")])
        # another feed — the mirror stays
        st["messages"]["m10"]["session"] = "w"
        self.assertEqual([(who, it.get("to")) for who, it in views._items(st, ev)], [("w", None), ("hub", "w")])

    def test_feed_line_says_who_sent_it(self):
        """API 1.26: a feed in line has the message's from {kind, name?}: a client folds core and module service lines
        by the field, not by the label text. For old records without the field — by the label (forms.parse_origin),
        without a label — the owner by the event flag, otherwise null."""
        st = {"sessions": {"hub": {"kind": "hub"}, "w": {}},
              "messages": {"m1": {"id": "m1", "session": "w", "text": "a", "from": {"kind": "module", "name": "voice"}},
                           "m2": {"id": "m2", "session": "w", "text": "b", "origin": f" {lang.legacy('from')} maps"},
                           "m3": {"id": "m3", "session": "w", "text": "c", "origin": ""},
                           "m4": {"id": "m4", "session": "hub", "text": "d", "from": {"kind": "core"}}},
              "delegations": {"d9": {"for": "m4", "session": "w"}}}
        got = [views._items(st, {"seq": n, "ts": 1.0, "type": "message.received", "src": "cli",
                                 "data": {"to": "w", "text": "x", **({"reply_to": "d9"} if n == 1 else {})}})
               for n in (1, 2, 3)]
        self.assertEqual([[(who, it["from"]) for who, it in g] for g in got], [
            [("w", {"kind": "module", "name": "voice"}), ("hub", {"kind": "module", "name": "voice"})],
            [("w", {"kind": "session", "name": "maps"})],
            [("w", None)]])   # no field, no label, no owner flag — unknown

    def test_owner_flag_off_his_channels_is_not_the_owner(self):
        """Without an owner label — as the fold decided (the message's from_owner), not by the event flag: an owner
        flag outside the owner's channels (a session message) does not make the sender the owner."""
        events = [{"type": "session.requested", "entity": "w", "data": {"session_id": "s", "cwd": "/w"}},
                  {"type": "message.received", "entity": None,
                   "data": {"to": "w", "text": "a", "owner": True, "channel": "session", "sender": "maps"}}]
        r = views.Reader()
        for n, e in enumerate(events, 1):
            r.feed({"seq": n, "ts": float(n), "src": "cli", **e})
        st = r.state
        self.assertFalse(st["messages"]["m2"]["from_owner"])
        st["messages"]["m2"].update(origin="", **{"from": None})   # no field and no label — only the fold's flag decides
        ev = {"seq": 2, "ts": 2.0, "type": "message.received", "src": "cli", "data": events[1]["data"]}
        self.assertIsNone(views._items(st, ev)[0][1]["from"])
        st["messages"]["m2"]["from_owner"] = True
        self.assertEqual(views._items(st, ev)[0][1]["from"], {"kind": "owner"})


class OwnerNewsMark(unittest.TestCase):
    """A result row that the core counts as new for the owner (owner_out) is marked news: the page counts "New · N" and
    places the "Last read" line from it, not from any answer after seen (an answer to the hub or a result sent to
    another session is not new)."""
    HUB = {"channel": "session", "owner": False, "sender": "hub"}

    def feed(self, raw):
        r = views.Reader()
        for ev in events(raw):
            r.feed(ev)
        return r

    def outs(self, r, name):
        return [(i["text"], bool(i.get("news"))) for i in r.timelines[name] if i["kind"] == "out"]

    def test_an_answer_to_the_owner_is_marked_and_an_answer_to_the_hub_is_not(self):
        r = self.feed([
            {"type": "session.requested", "entity": "hub", "data": {"session_id": "sh", "cwd": "/w", "kind": "hub", "channel": "web", "owner": True}},
            {"type": "session.started", "entity": "hub", "data": {"session_id": "sh"}},
            {"type": "session.requested", "entity": "a", "data": {"session_id": "s", "cwd": "/w", "channel": "web", "owner": True}},
            {"type": "session.started", "entity": "a", "data": {"session_id": "s"}},
            # an answer to the owner
            {"type": "message.received", "data": {"to": "a", "text": "what is new?", "channel": "web", "owner": True}},
            {"type": "turn.started", "entity": "a", "data": {"marker": "m5"}},
            {"type": "session.said", "entity": "a", "data": {"text": "Intermediate"}},
            {"type": "session.said", "entity": "a", "data": {"text": "Answer to the owner"}},
            {"type": "turn.result", "entity": "a", "data": {"text": "Answer to the owner"}},
            # a hub task: the result is REPLIED to the hub, not addressed to the owner
            {"type": "delegation.created", "data": {"session": "a", "for_session": "hub", "context": "collect the log", **self.HUB}},
            {"type": "turn.started", "entity": "a", "data": {"marker": "m10"}},
            {"type": "session.said", "entity": "a", "data": {"text": "Log collected"}},
            {"type": "turn.result", "entity": "a", "data": {"text": "Log collected"}},
        ])
        self.assertEqual(self.outs(r, "a"), [("Intermediate", False), ("Answer to the owner", True), ("Log collected", False)])


if __name__ == "__main__":
    unittest.main()
