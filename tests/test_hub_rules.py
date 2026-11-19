"""Hub rules for every session (everyone writes to a chat only through one agent; agents get
that in their CLAUDE.md, but there was nowhere to tell the hub, executors and working sessions). `rules` in hub.json is a
list of strings: the core puts them in front of every session next to the list of agents. No setting, no change."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import tempfile
import unittest

from hosts.streamjson import StreamHost
from tests.fakes import lang

RU_WORD = next(k for k, v in lang.legacy("lang_codes").items() if v == "ru")   # Russian as the earlier core named it
RU_TEXT = lang.LETTERS["ru"]   # an owner's message the core takes for Russian (the language data's alphabet)


class HubRules(unittest.TestCase):
    def host(self, cfg):
        state = tempfile.mkdtemp(prefix="rules-")
        path = os.path.join(state, "hub.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        old = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = path
        self.addCleanup(lambda: os.environ.__setitem__("HUB_CONFIG", old) if old else os.environ.pop("HUB_CONFIG"))
        return StreamHost(state, claude=("echo",))

    def prompt(self, cmd):
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            return f.read()

    def test_rules_reach_a_plain_session(self):
        rule = "Write to people in chat only through the messenger agent: hub send messenger \"<to whom, what>\"."
        text = self.prompt(self.host({"rules": [rule]}).command("worker", "s-w"))
        self.assertIn("The rules of this hub", text)
        self.assertIn(rule, text)

    def test_rules_sit_next_to_the_roster(self):
        cmd = self.host({"rules": ["a rule"]}).command("worker", "s-w", agents=[["messenger", "chats"]])
        text = self.prompt(cmd)
        self.assertIn("- messenger — chats", text)
        self.assertIn("- a rule", text)

    def test_no_rules_no_section(self):
        self.assertNotIn("The rules of this hub", self.prompt(self.host({}).command("worker", "s-w")))
        self.assertNotIn("The rules of this hub", self.prompt(self.host({"rules": ["", "  "]}).command("worker", "s-w")))

    def test_every_session_keeps_the_owner_language_and_its_context_through_compaction(self):
        # 2026-10-09: after a compact some sessions answered in English: the Claude Code summary and everything around
        # it is in English, and the language was held only by the conversation itself. A rule in the system
        # instructions survives compaction, and the summary (/compact, auto-compact) takes it into account: a probe on
        # a copy of a conversation gave "The owner writes in Russian, so I answer…"
        text = self.prompt(self.host({}).command("worker", "s-w"))
        self.assertIn("in the owner's language", text)
        self.assertIn("auto-compact", text)


    def test_every_session_checks_its_background_before_starting_another(self):
        # 2026-10-11, owner: background waits piled up, 4-5 identical ones; eval: a rule in the hub skill does not get
        # through (the skill is not opened, 1/3), in the system prompt addendum it does (3/3)
        text = self.prompt(self.host({}).command("worker", "s-w"))
        self.assertIn("run_in_background", text)
        self.assertIn("TaskStop", text)

    def test_every_session_reads_a_short_value_after_a_task_as_a_correction(self):
        # 2026-10-12, owner: a bare "Two." after "put a.txt with 'one'" produced a second file next to it; eval: without
        # the rule 1/6, with the rule in the addendum 6/6, and additions ("And also X") are still both done
        text = self.prompt(self.host({}).command("worker", "s-w"))
        self.assertIn("is a correction to that task", text)
        self.assertIn("do not make a second one next to it", text)
        self.assertIn('"also"', text)          # an addition is not a correction

    def test_every_session_acts_on_the_first_request(self):
        # from the owner's personal setup (2026-10-14): a session explained why it "can't" and re-asked what he had already asked
        text = self.prompt(self.host({}).command("worker", "s-w"))
        self.assertIn("do it at once", text)
        self.assertIn("do not ask again what they already asked", text)

    def test_every_session_answers_a_question_without_changing_things(self):
        # 2026-10-16, friction summary: "did more than asked" 20+ times, "I only asked"; eval: "is it possible to…"
        # without the line 0/5, with it 7/7. The line sits in the first_action part: a module (prompt.parts) sees it there
        line = "without a request to act — an answer in text"
        for kw in ({}, {"agents": [["maps", "maps"]]}, {"executor_of": "maps"}):
            self.assertIn(line, self.prompt(self.host({}).command("worker", "s-w", **kw)))
        from tests.test_modapi_prompt import Stub
        mods = Stub(m=lambda p, d: None)
        h = self.host({})
        h.modules = mods
        h.command("worker", "s-w")
        parts = {p["name"]: p["text"] for p in mods.calls[0][2]["parts"]}
        self.assertIn(line, parts["first_action"])
        # an imperative is a command, not a question: "fix", "run", "publish" must not be read as "answer in text"
        self.assertIn('"fix it", "run it", "publish it"', parts["first_action"])
        self.assertIn("publishing and a rollout — only on their word", parts["first_action"])

    def test_every_session_rechecks_shared_state_before_relying_on_it(self):
        # 2026-10-14: a fact that parallel sessions change (hub, skills, settings) is often stale in the summary after compaction
        text = self.prompt(self.host({}).command("worker", "s-w"))
        self.assertIn("re-read the source of truth", text)
        self.assertIn("after compaction", text)

    def test_a_rule_change_from_another_session_is_not_the_owners_yes(self):
        # 2026-10-14 (on 10-02 a relayed task reached the wrong session): a change to a session's own instructions made
        # on another session's say-so is confirmed by the owner
        text = self.prompt(self.host({}).command("worker", "s-w"))
        self.assertIn("is not the owner's \"yes\": ask the owner directly", text)
        self.assertIn("do not add rules", text)

    def test_autonomy_follows_the_owners_words_and_never_moves_the_hard_gates(self):
        # 2026-10-14: the level of autonomy is set by the owner's words; the irreversible and the gates always need his "yes"
        text = self.prompt(self.host({}).command("worker", "s-w"))
        self.assertIn('"finish it yourself"', text)
        self.assertIn("do not answer your own question for them", text)

    def test_findings_along_the_way_are_reported_then_pursued(self):
        # 2026-10-14: findings along the way were lost when mentioned in passing: the answer on the task first, then the finding
        text = self.prompt(self.host({}).command("worker", "s-w"))
        self.assertIn("first answer on your own task", text)
        self.assertIn("goes to them as options", text)

    def test_every_session_but_the_liaison_records_addresses_it_sees(self):
        # 2026-11-06 (owner decision): the books were empty — the rule lived in the hub skill only; recording is not a
        # permission to write to the person
        h = self.host({})
        for kw in ({}, {"executor_of": "dev"}, {"bus": "dev"}):
            with self.subTest(**kw):
                text = self.prompt(h.command("worker", "s-w", **kw))
                self.assertIn('hub contacts add "<full name>"', text)
                self.assertIn("--source", text)
                self.assertIn("no permission to write to anyone", text)
        self.assertNotIn("hub contacts", self.prompt(h.command("liaison", "s-l", liaison=True)))

    def test_the_owner_language_is_named(self):
        # 2026-10-09: the owner hardly ever writes to the secretary, so it took "the language of whoever writes to you"
        # from the English summary and the general rules and answered in English after a compact; the core knows the
        # owner's language from his messages and names it
        text = self.prompt(self.host({}).command("worker", "s-w", lang=RU_WORD))
        self.assertIn("The owner's language is Russian", text)
        self.assertNotIn("The owner's language is", self.prompt(self.host({}).command("worker", "s-w")))


class OwnerLanguage(unittest.TestCase):
    def test_the_core_keeps_the_owner_language_from_his_messages_and_gives_it_to_every_spawn(self):
        from core import reducer
        st, seq = reducer.empty(), 0

        def ev(type, entity=None, **data):
            nonlocal st, seq
            seq += 1
            st, acts, why = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "type": type, "entity": entity,
                                                "data": data})
            self.assertIsNone(why)
            return acts
        ev("session.requested", "a", cwd="/w", session_id="s-a", channel="web", owner=True)
        ev("session.started", "a")
        ev("message.received", None, to="a", text=RU_TEXT, channel="web", owner=True)
        ev("message.received", None, to="a", text="ok", channel="telegram", owner=True)   # a short reply does not change the language
        ev("message.received", None, to="a", text="Please check the build, it fails again", channel="session",
           owner=False, sender="a")                                                      # not the owner: does not count
        self.assertEqual(st["owner"].get("lang"), "ru")   # a language code (core/forms.py; before 10-17 it was the language name)
        ev("session.exited", "a", why="test")
        (spawn,) = [a for a in ev("message.received", None, to="a", text="once more", channel="web", owner=True)
                    if a["do"] == "spawn"] or [reducer.spawn_action(st["sessions"]["a"], st=st)]
        self.assertEqual(spawn["lang"], "ru")
        ev("message.received", None, to="a", text=RU_TEXT + " "
           "https://git.example.com/someone/time-clock-landing/-/merge_requests/1", channel="web", owner=True)
        self.assertEqual(st["owner"]["lang"], "ru")   # 2026-10-09: a link is not speech
        for _ in range(3):   # one message in English does not switch the language, a steady run does
            ev("message.received", None, to="a", text="Let us switch to English for this project, please", channel="web",
               owner=True)
        self.assertEqual(st["owner"]["lang"], "latin")   # Latin script (from languages, slice S1); the rule gets "English"
        self.assertEqual(reducer.spawn_action(st["sessions"]["a"], st=st)["lang"], "en")

    def test_known_from_earlier_messages_before_any_new_one(self):
        # 2026-10-09: after a restart the owner had not yet written, so the language was not named and the secretary
        # answered in English again; his earlier messages are in the state, so the language is taken from them
        from core import reducer
        st = reducer.empty()
        st["sessions"]["a"] = {"name": "a", "session_id": "s-a", "cwd": "/w", "state": "asleep"}
        st["messages"]["m1"] = {"id": "m1", "session": "a", "from_owner": True, "created": 1,
                                "text": RU_TEXT}
        st["messages"]["m2"] = {"id": "m2", "session": "a", "created": 2, "text": "Please check the build, it fails"}
        self.assertEqual(reducer.spawn_action(st["sessions"]["a"], st=st)["lang"], "ru")


if __name__ == "__main__":
    unittest.main()
