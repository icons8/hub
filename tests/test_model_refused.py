"""Opus's safety filter stopped an answer and Claude Code went on with Opus 4.8 to the end of the process; a plain
--resume kept it there (it restores the last answer's model and latches the fallback). A hub session ran on Opus 4.8
for a month and the owner never saw it. The core brings it back on the asked model, with the stopped turn cut out of the
conversation, once per trigger (core/refusals.py, hosts/streamjson.py). The stream lines below are what Claude Code
2.1.309 printed against a fake API that refused twice."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import tempfile
import unittest
from unittest import mock

from core import refusals, reducer, views
from hosts import streamjson, worktree
from hosts.common import transcript_entry

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
DAY = refusals.WINDOW


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("maps", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)
        self.turn()

    def ev(self, type, entity=None, when=None, **data):
        self.seq += 1
        self.ts = when if when is not None else 100 + self.seq
        e = {"seq": self.seq, "ts": self.ts, "type": type, "entity": entity, "data": data}
        self.events = getattr(self, "events", []) + [e]
        self.st, actions, why = reducer.reduce(self.st, e)
        self.why = why
        return [a for a in actions if a.get("session") == "maps"]

    def turn(self, text="build the tiles", when=None):
        self.ev("message.received", None, to="maps", text=text, when=when, **OWNER)
        mid = f"m{self.seq}"
        self.ev("turn.started", "maps", marker=mid, when=when)
        return mid

    def refused(self, when=None, **over):
        return self.ev("session.model_refused", "maps", when=when, **{"asked": "claude-opus-5-5", "got": "claude-opus-4-8",
                                                                  "at": "aaaa1111-0000-0000-0000-000000000001",
                                                                  "category": "cyber", **over})

    def notes(self):
        return [m for m in self.st["messages"].values() if m["session"] == "maps" and m.get("fyi")]

    def view(self, now=None):
        return refusals.view(self.st, self.st["sessions"]["maps"], self.ts if now is None else now)

    def test_back_on_the_asked_model_after_the_turn_with_the_turn_cut(self):
        mid = f"m{self.seq - 1}"
        self.assertEqual(self.refused(), [])                             # mid-turn: nothing yet
        self.assertEqual(self.view()["state"], "waits")
        acts = self.ev("turn.stopped", "maps")
        self.assertEqual([a["do"] for a in acts], ["release", "spawn"])
        spawn = acts[1]
        self.assertEqual((spawn["resume"], spawn["resume_at"], spawn["back_model"]),
                         (True, "aaaa1111-0000-0000-0000-000000000001", "claude-opus-5-5"))
        self.ev("host.done", "maps", do="spawn", result="spawned", cut=True)   # the host found the entry and cut
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        (note,) = self.notes()
        self.assertIn(mid, note["text"])
        self.assertIn("removed that turn", note["text"])
        self.assertIn(f"`hub show {mid}`", note["text"])                # where the words of the removed turn are
        self.assertTrue(forms_core(note))
        self.assertEqual((self.view()["state"], self.view()["cut"]), ("back", True))
        self.ev("session.info", "maps", model="claude-opus-5-5")         # the new process is on the asked model
        self.assertEqual(self.view()["state"], "back")
        # the next raise is a plain one: the cut is applied once
        self.ev("session.exited", "maps", why="reconcile")
        self.assertEqual(self.ev("session.wake_requested", "maps", **OWNER), [])
        spawn = next((a for a in self.ev("session.reload_requested", "maps", **OWNER) if a["do"] == "spawn"), None)
        self.assertTrue(spawn is None or "resume_at" not in spawn)

    def test_an_open_card_holds_the_return(self):
        self.ev("permission.requested", "maps", request_id="r1", tool="Bash", input={"command": "ls"})
        self.assertEqual(self.refused(), [])
        self.assertEqual(self.st["sessions"]["maps"]["state"], "dialog")   # waits for the owner: not cut
        acts = self.ev("permission.answered", "maps", request_id="r1", answer="allow", **OWNER)
        self.assertEqual([a["do"] for a in acts if a["do"] in ("release", "spawn")], [])
        acts = self.ev("turn.stopped", "maps")
        self.assertEqual([a["do"] for a in acts if a["do"] in ("release", "spawn")], ["release", "spawn"])

    def test_background_work_holds_it_a_message_does_not(self):
        self.ev("session.background", "maps", id="b1", kind="bash", title="tests")
        self.refused()
        self.assertEqual(self.ev("turn.stopped", "maps"), [])            # its tests are running: not cut
        acts = self.ev("message.received", None, to="maps", text="and then?", **OWNER)
        self.assertEqual([a["do"] for a in acts], ["release", "spawn"])  # a message goes to the new process anyway

    def test_a_repeat_soon_after_the_return_is_only_shown(self):
        self.refused()
        self.ev("turn.stopped", "maps")
        self.ev("host.done", "maps", do="spawn", result="spawned", cut=True)
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        back = self.ts
        self.turn()
        self.assertEqual(self.refused(), [])
        self.assertEqual(self.ev("turn.stopped", "maps"), [])            # no second automatic return
        self.assertEqual(self.view()["state"], "again")
        self.assertFalse(self.st["sessions"]["maps"].get("dirty"))
        # a crash raises it as it is: no return without someone's hand
        spawn = next(a for a in self.ev("session.exited", "maps", why="process gone") if a["do"] == "spawn")
        self.assertNotIn("back_model", spawn)
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        # the owner's reload cuts: a plain --resume would stay on Opus 4.8
        spawn = next(a for a in self.ev("session.reload_requested", "maps", **OWNER) if a["do"] == "spawn")
        self.assertEqual(spawn["resume_at"], "aaaa1111-0000-0000-0000-000000000001")
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        self.assertEqual(len(self.notes()), 2)
        # a day later it is a new trigger: returned by itself again
        self.turn(when=back + DAY + 10)
        self.refused(when=back + DAY + 11)
        acts = self.ev("turn.stopped", "maps", when=back + DAY + 12)
        self.assertEqual([a["do"] for a in acts], ["release", "spawn"])

    def test_any_next_raise_carries_the_cut(self):
        # the process went away mid-turn (a crash, a hub restart): the core's own raise cuts too
        self.refused()
        acts = self.ev("session.exited", "maps", why="process gone")
        spawn = next(a for a in acts if a["do"] == "spawn")
        self.assertEqual(spawn["resume_at"], "aaaa1111-0000-0000-0000-000000000001")
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        self.assertEqual(len(self.notes()), 1)

    def test_codex_and_same_model_are_not_touched(self):
        self.st["codex"] = {"host": True}
        self.st["sessions"]["maps"]["model"] = "codex/gpt"
        self.refused()
        self.assertNotIn("refused", self.st["sessions"]["maps"])
        self.st["sessions"]["maps"].pop("model")
        self.refused(got="claude-opus-5-5")
        self.refused(asked="rm -rf /")
        self.assertNotIn("refused", self.st["sessions"]["maps"])
        self.assertEqual(self.ev("turn.stopped", "maps"), [])

    def test_without_a_cut_point_the_model_alone(self):
        self.refused(at=None)
        spawn = self.ev("turn.stopped", "maps")[1]
        self.assertNotIn("resume_at", spawn)
        self.assertEqual(spawn["back_model"], "claude-opus-5-5")
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        self.assertIn("same conversation", self.notes()[0]["text"])

    def test_a_raise_without_a_conversation_on_disk_drops_it(self):
        self.st["sessions"]["maps"]["has_history"] = False
        self.refused()
        spawn = self.ev("turn.stopped", "maps")[1]
        self.assertNotIn("back_model", spawn)
        self.assertNotIn("refused", self.st["sessions"]["maps"])

    def test_a_turn_the_session_began_itself_is_a_trigger_too(self):
        # a turn with no message (a background task ended) has no turn_head: the first fact is not "the same trigger"
        self.ev("turn.stopped", "maps")
        self.ev("turn.started", "maps", marker=None)
        self.st["sessions"]["maps"].pop("turn_head", None)
        self.refused()
        self.assertEqual(self.view()["state"], "waits")

    def test_the_notice_after_the_answer_adds_its_cut_to_the_same_trigger(self):
        self.refused(at=None, via="answer")                              # by the answer's model: no cut yet
        self.refused(via="notice")                                       # Claude Code's notice of the same turn
        self.assertEqual(self.st["sessions"]["maps"]["refused"]["at"], "aaaa1111-0000-0000-0000-000000000001")
        self.assertEqual(self.view()["day"], 1)                          # one trigger, not two
        self.assertEqual(self.ev("turn.stopped", "maps")[1]["resume_at"], "aaaa1111-0000-0000-0000-000000000001")

    def test_a_notice_without_a_cut_is_still_the_same_trigger(self):
        self.refused(at=None, via="answer")
        self.refused(at=None, via="notice")                              # its prompt was not found on disk
        self.assertEqual(self.view()["day"], 1)

    def test_a_session_does_not_bring_itself_back_after_a_repeat(self):
        self.refused()
        self.ev("turn.stopped", "maps")
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        self.turn()
        self.refused()                                                   # again
        self.ev("turn.stopped", "maps")
        spawn = next(a for a in self.ev("session.reload_requested", "maps", channel="session", owner=False,
                                        sender="maps") if a["do"] == "spawn")
        self.assertNotIn("back_model", spawn)                            # its own reload: as it is
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        self.ev("session.info", "maps", model="claude-opus-4-8")
        spawn = next(a for a in self.ev("session.reload_requested", "maps", **HUB) if a["do"] == "spawn")
        self.assertEqual(spawn["back_model"], "claude-opus-5-5")         # the hub's: back

    def test_a_turn_on_a_later_message_keeps_the_conversation(self):
        # a repeat the core does not raise: the owner works on with it, a cut at the stopped turn would take that work
        # too - his reload brings only the model back
        self.refused()
        self.ev("turn.stopped", "maps")
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        self.turn()
        self.refused()                                                   # again: shown, not raised
        self.ev("turn.stopped", "maps")
        self.turn("go on with the tiles")
        self.assertIsNone(self.st["sessions"]["maps"]["refused"]["at"])
        self.ev("turn.stopped", "maps")
        spawn = next(a for a in self.ev("session.reload_requested", "maps", **OWNER) if a["do"] == "spawn")
        self.assertNotIn("resume_at", spawn)
        self.assertEqual(spawn["back_model"], "claude-opus-5-5")
        self.assertFalse(self.view()["cut"])

    def test_still_on_the_fallback_after_the_return_is_a_repeat(self):
        # Claude Code kept the fallback after the raise (its first init names it): not "back" - a repeat, shown
        self.refused()
        self.ev("turn.stopped", "maps")
        self.ev("host.done", "maps", do="spawn", result="spawned", cut=True)
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        self.ev("session.info", "maps", model="claude-opus-4-8")
        self.assertEqual(self.view()["state"], "again")
        self.assertIsNone(self.st["sessions"]["maps"]["refused"]["at"])   # cut already: the next return is the model
        self.assertFalse(self.st["sessions"]["maps"].get("dirty"))

    def test_a_raise_that_dies_before_its_start_does_not_cut_again(self):
        # a Claude Code that does not know --resume-session-at fails every launch: after two, the raise is model-only;
        # one exit may be the old process a reload killed - the cut stays
        self.refused()
        self.ev("turn.stopped", "maps")
        spawn = next(a for a in self.ev("session.exited", "maps", why="process gone") if a["do"] == "spawn")
        self.assertEqual(spawn["resume_at"], "aaaa1111-0000-0000-0000-000000000001")
        spawn = next(a for a in self.ev("session.exited", "maps", why="process gone") if a["do"] == "spawn")
        self.assertNotIn("resume_at", spawn)
        self.assertEqual(spawn["back_model"], "claude-opus-5-5")
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        self.assertEqual(self.view()["state"], "back")

    def test_the_note_and_the_page_say_what_the_host_did(self):
        self.refused()
        self.ev("turn.stopped", "maps")
        self.ev("host.done", "maps", do="spawn", result="spawned", cut=False)   # the entry was not on disk
        self.ev("session.started", "maps", session_id="s-maps", source="resume")
        self.assertIn("same conversation", self.notes()[0]["text"])
        self.assertFalse(self.view()["cut"])

    def test_a_new_conversation_drops_it(self):
        self.refused()
        self.ev("session.started", "maps", session_id="s-other", source="clear")
        self.assertNotIn("refused", self.st["sessions"]["maps"])

    def test_the_account_day_count(self):
        self.refused()
        self.assertEqual(self.view()["day"], 1)
        self.st["sessions"]["maps"].pop("refused")
        self.ev("session.model_refused", "maps", asked="claude-opus-5-5", got="claude-opus-4-8")
        self.assertEqual(self.view()["day"], 2)
        self.assertEqual(self.view(self.ts + DAY - 1)["day"], 1)          # the first one left the day
        self.assertEqual(self.view(self.ts + DAY + 1)["day"], 0)

    def test_the_snapshot_row_has_it(self):
        self.refused()
        r = views.Reader()
        for e in self.events:
            r.feed(e)
        rows = {x["name"]: x for x in r.snapshot(now=self.ts)["sessions"]}
        self.assertEqual({k: rows["maps"]["refused"][k] for k in ("state", "asked", "got", "day")},
                         {"state": "waits", "asked": "claude-opus-5-5", "got": "claude-opus-4-8", "day": 1})
        self.assertIsNone(rows["hub"]["refused"])

    def test_only_the_host_writes_it(self):
        from core import admit
        self.assertIn("session.model_refused", admit.CORE_ONLY)


def forms_core(m):
    from core import forms
    return forms.is_core(m)


class Host(unittest.TestCase):
    """What Claude Code prints and what the host makes of it."""
    PROMPT = "ce4a7bac-ee21-44c2-933b-5fbaa7933d5d"
    BEFORE = "af98e05c-340a-421d-a8c3-62bae0ae0c8e"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="refused-")
        self.cfg = os.path.join(self.dir, "cfg")
        self.cwd = os.path.join(self.dir, "maps")
        os.makedirs(self.cwd)
        proj = os.path.join(self.cfg, "projects", worktree.slug(self.cwd))
        os.makedirs(proj)
        rows = [{"type": "user", "uuid": "11111111-0000-0000-0000-000000000001", "parentUuid": None,
                 "message": {"content": "hello"}},
                {"type": "assistant", "uuid": self.BEFORE, "parentUuid": "11111111-0000-0000-0000-000000000001",
                 "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": f"see {self.PROMPT}"}]}},
                {"type": "user", "uuid": self.PROMPT, "parentUuid": self.BEFORE, "message": {"content": "go on"}},
                {"type": "system", "subtype": "model_refusal_fallback", "uuid": "73983ea7-0000-0000-0000-000000000001",
                 "parentUuid": self.PROMPT}]
        with open(os.path.join(proj, "s-maps.jsonl"), "w") as f:
            f.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        self.host = streamjson.StreamHost(self.dir, claude=["claude"])
        self.host.fresh_prompt = False
        proc = streamjson.StreamProcess(mock.MagicMock())
        proc.session_id, proc.mcp_spawn = "s-maps", {"cwd": self.cwd}
        self.host.procs["maps"] = proc
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg})
        env.start()
        self.addCleanup(env.stop)

    def line(self, msg):
        return [f for f in self.host._line("maps", msg) if f[0] == "session.model_refused"]

    def init(self, model):
        return self.line({"type": "system", "subtype": "init", "model": model, "session_id": "s-maps"})

    def test_the_notice_gives_the_fact_with_the_cut_point(self):
        self.init("claude-opus-5-5")
        notice = {"type": "system", "subtype": "model_refusal_fallback", "trigger": "refusal", "direction": "retry",
                  "scope": "session", "original_model": "claude-opus-5-5", "fallback_model": "claude-opus-4-8",
                  "api_refusal_category": "cyber", "refused_user_message_uuid": self.PROMPT,
                  "session_id": "s-maps", "uuid": "73983ea7-0000-0000-0000-000000000001"}
        ((_, _, data),) = self.line(notice)
        self.assertEqual(data, {"asked": "claude-opus-5-5", "got": "claude-opus-4-8", "at": self.BEFORE,
                                "category": "cyber", "via": "notice"})
        self.init("claude-opus-4-8")                      # every next turn's init names the fallback
        self.assertEqual(self.line({"type": "assistant", "message": {"model": "claude-opus-4-8", "content": []}}), [])
        self.assertEqual(self.line(notice), [])           # once per process

    def test_a_helper_agents_fallback_is_not_the_sessions(self):
        self.init("claude-opus-5-5")
        self.assertEqual(self.line({"type": "system", "subtype": "model_refusal_fallback", "scope": "local",
                                    "original_model": "claude-opus-5-5", "fallback_model": "claude-opus-4-8"}), [])
        self.assertEqual(self.line({"type": "assistant", "parent_tool_use_id": "t1",
                                    "message": {"model": "claude-haiku-5-5", "content": []}}), [])

    def test_an_answer_from_another_model_without_the_notice(self):
        self.init("claude-opus-5-5[1m]")
        self.assertEqual(self.line({"type": "assistant", "message": {"model": "claude-opus-5-5", "content": []}}), [])
        self.assertEqual(self.line({"type": "assistant", "message": {"model": "<synthetic>", "content": []}}), [])
        ((_, _, data),) = self.line({"type": "assistant", "message": {"model": "claude-opus-4-8", "content": []}})
        self.assertEqual((data["asked"], data["got"], data["at"]), ("claude-opus-5-5[1m]", "claude-opus-4-8", None))

    def test_the_owners_model_switch_in_the_process_is_not_the_filter(self):
        # `/model claude-opus-4-8` typed into a live session: Claude Code switches in place, the next turn's init
        # names the new model before its answer
        self.init("claude-opus-5-5")
        self.assertEqual(self.line({"type": "assistant", "message": {"model": "claude-opus-5-5", "content": []}}), [])
        self.init("claude-opus-4-8")
        self.assertEqual(self.line({"type": "assistant", "message": {"model": "claude-opus-4-8", "content": []}}), [])
        # the filter on the model now asked still counts
        ((_, _, data),) = self.line({"type": "assistant", "message": {"model": "claude-opus-4-7", "content": []}})
        self.assertEqual((data["asked"], data["got"]), ("claude-opus-4-8", "claude-opus-4-7"))

    def test_another_family_is_someones_choice_not_the_filter(self):
        self.init("claude-opus-5-5")
        self.assertEqual(self.line({"type": "assistant", "message": {"model": "claude-sonnet-5-5", "content": []}}), [])

    def test_the_notice_after_the_answer_brings_the_cut_and_the_1m_mark_stays(self):
        self.init("claude-opus-5-5[1m]")
        ((_, _, first),) = self.line({"type": "assistant", "message": {"model": "claude-opus-4-8", "content": []}})
        self.assertIsNone(first["at"])
        ((_, _, data),) = self.line({"type": "system", "subtype": "model_refusal_fallback", "scope": "session",
                                     "original_model": "claude-opus-5-5", "fallback_model": "claude-opus-4-8",
                                     "refused_user_message_uuid": self.PROMPT, "session_id": "s-maps"})
        self.assertEqual((data["asked"], data["at"]), ("claude-opus-5-5[1m]", self.BEFORE))
        self.assertEqual(self.line({"type": "assistant", "message": {"model": "claude-opus-4-8", "content": []}}), [])

    def test_an_unavailable_model_is_not_the_filter(self):
        self.init("claude-opus-5-5")
        self.assertEqual(self.line({"type": "system", "subtype": "model_fallback", "original_model": "claude-opus-5-5",
                                    "fallback_model": "claude-opus-4-8"}), [])
        self.assertEqual(self.line({"type": "assistant", "message": {"model": "claude-opus-4-8", "content": []}}), [])
        # the filter's own notice later in the same process still counts
        ((_, _, data),) = self.line({"type": "system", "subtype": "model_refusal_fallback", "scope": "session",
                                     "original_model": "claude-opus-5-5", "fallback_model": "claude-opus-4-8",
                                     "refused_user_message_uuid": self.PROMPT, "session_id": "s-maps"})
        self.assertEqual(data["at"], self.BEFORE)

    def test_an_alias_at_init_is_not_compared(self):
        self.init("opus")
        self.assertEqual(self.line({"type": "assistant", "message": {"model": "claude-opus-4-8", "content": []}}), [])

    def test_the_entry_is_found_from_the_end_and_not_by_a_mention(self):
        self.assertEqual(transcript_entry(self.cwd, "s-maps", self.PROMPT, claude_dir=self.cfg)["parentUuid"],
                         self.BEFORE)
        self.assertIsNone(transcript_entry(self.cwd, "s-maps", "00000000-0000-0000-0000-000000000000",
                                           claude_dir=self.cfg))
        self.assertIsNone(transcript_entry(self.cwd, "s-none", self.PROMPT, claude_dir=self.cfg))

    def spawn(self, **kw):
        popen = mock.MagicMock()
        popen.poll.return_value = None
        self.host.procs.clear()
        flags = []
        self.host.tool_flags = lambda **k: flags.append(k.get("model")) or (["--model", k["model"]] if k.get("model")
                                                                            else [])
        with mock.patch.object(streamjson.subprocess, "Popen", return_value=popen) as p, \
                mock.patch.object(streamjson.threading, "Thread"), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg}):
            self.host.spawn("maps", "s-maps", resume=True, cwd=self.cwd, **kw)
        return p.call_args.args[0]

    def test_the_raise_cuts_only_at_an_entry_that_is_there(self):
        cmd = self.spawn(resume_at=self.BEFORE, back_model="claude-opus-5-5")
        self.assertEqual(cmd[cmd.index("--resume-session-at") + 1], self.BEFORE)
        self.assertEqual(self.host.refusal_cut, {"maps": True})          # hubd puts it into host.done
        self.assertNotIn("--model", cmd)                  # the cut drops the latch: Claude Code picks the model itself
        # an unknown entry fails Claude Code's launch ("No message found"): no cut, the asked model by name
        cmd = self.spawn(resume_at="00000000-0000-0000-0000-000000000000", back_model="claude-opus-5-5")
        self.assertNotIn("--resume-session-at", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(self.host.refusal_cut, {"maps": False})
        cmd = self.spawn(resume_at="00000000-0000-0000-0000-000000000000", back_model="claude-opus-5-5",
                         model="claude-sonnet-5-5")       # its own model wins
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")
        self.assertNotIn("--resume-session-at", self.spawn())


if __name__ == "__main__":
    unittest.main()
