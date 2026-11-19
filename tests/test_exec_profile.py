"""A compressed agent profile for its executors: next to the agent's CLAUDE.md — CLAUDE.executor.md
(first line `source-sha256: <hash>`) and CLAUDE.executor.src.md (the full profile it was compressed from). The agent's
executor gets the digest while CLAUDE.md is unchanged; only appended lines — the digest plus them verbatim at the end;
removed or changed lines, a large addition, a mismatched pair — the full profile and a stale mark in the journal
(host.done of the spawn) so it gets recompressed. The agent itself always gets the full one; no pair — as before."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import hashlib
import json
import os
import re
import tempfile
import types
import unittest

from core import execprofile
from hosts.streamjson import StreamHost

# a long part the digest leaves out: a delta pays only while the digest plus it stays shorter than the full profile
BACK = "## Background\n" + "".join(f"- note {i}: " + "x" * 80 + "\n" for i in range(30))
FULL = ("# Agent\n\n## Knowledge\n- rule one\n- rule two\n\n## Lessons\n- lesson one\n- lesson two\n\n" + BACK)
SHORT = "# Agent (digest)\n\n- rule one\n- lesson one\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="execprof-")
        cfg = os.path.join(self.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({}, f)
        old = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = cfg
        self.addCleanup(lambda: os.environ.__setitem__("HUB_CONFIG", old) if old else os.environ.pop("HUB_CONFIG"))
        self.host = StreamHost(self.state, claude=("echo",))
        self.host.fresh_prompt = False
        self.dir = os.path.join(self.state, "agents", "p")
        os.makedirs(self.dir)
        self.file = os.path.join(self.dir, "CLAUDE.md")
        self.write("CLAUDE.md", FULL)

    def write(self, name, text):
        # newline="": the bytes as given — in text mode Windows writes CRLF, and the hash in pair() is over LF
        with open(os.path.join(self.dir, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def pair(self, src=FULL, short=SHORT, sha=None):
        self.write("CLAUDE.executor.src.md", src)
        sha = sha or hashlib.sha256(src.encode()).hexdigest()
        self.write("CLAUDE.executor.md", f"source-sha256: {sha}\n\n{short}")

    def prompt(self, executor_of="maps"):
        cmd = self.host.command("w", "s-1", agent_file=self.file, executor_of=executor_of)
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            return f.read()

    def used(self):
        return self.host.profile_used.get("w")


class Executor(Base):
    def test_no_pair_gives_the_full_profile_as_before(self):
        text = self.prompt()
        self.assertTrue(text.startswith(FULL.rstrip("\n") + "\n\n"))
        self.assertNotIn("digest", text)
        self.assertIsNone(self.used())

    def test_an_unchanged_profile_gives_the_short_one(self):
        self.pair()
        text = self.prompt()
        self.assertTrue(text.startswith(SHORT.rstrip("\n") + "\n\n"))
        self.assertNotIn("source-sha256", text)
        self.assertNotIn("lesson two", text)
        self.assertIn(self.file, text)                                 # where the full one is — in the executor's note
        self.assertEqual(self.used()["kind"], "executor")
        self.assertNotIn("stale", self.used())

    def test_added_lines_go_verbatim_after_the_short_one_under_their_section(self):
        self.pair()
        self.write("CLAUDE.md", FULL.replace("- lesson two\n", "- lesson two\n- lesson three: `hub x --y`\n"))
        text = self.prompt()
        self.assertTrue(text.startswith(SHORT.rstrip("\n")))
        self.assertIn("- lesson three: `hub x --y`", text)
        self.assertIn("## Lessons", text.split(SHORT.rstrip("\n"), 1)[1])   # the section where the addition sits
        self.assertNotIn("lesson two", text)
        self.assertEqual(self.used()["kind"], "executor+delta")

    def test_a_changed_line_goes_verbatim_after_the_short_one_with_the_start_of_its_item(self):
        src = FULL.replace("- rule two\n", "- rule two: deploy with make\n  then check the page\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("  then check the page\n", "  then check the page and the log\n"))
        text = self.prompt()
        self.assertTrue(text.startswith(SHORT.rstrip("\n")))
        delta = text.split(execprofile.DELTA_HEAD, 1)[1]
        self.assertIn("  then check the page and the log", delta)
        self.assertIn("- rule two: deploy with make", delta)            # the anchor: the start of the item
        self.assertIn("## Knowledge", delta)                              # and its section
        self.assertNotIn("lesson two", text)
        self.assertEqual(self.used()["kind"], "executor+delta")

    def test_a_rewrapped_paragraph_that_keeps_its_gate_phrases_goes_into_the_delta(self):
        src = FULL.replace("- rule two\n", "- rule two: never push to main,\n  ask the owner first\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("- rule two: never push to main,\n  ask the owner first\n",
                                            "- rule two: never push to main, ask the owner\n  first, and say why\n"))
        self.assertIn("  first, and say why", self.prompt())
        self.assertEqual(self.used()["kind"], "executor+delta")

    def test_a_replacement_that_drops_a_gate_phrase_gives_the_full_profile_and_marks_stale(self):
        for old, new in (("- rule two: never push to main\n", "- rule two: push to main when green\n"),
                         ("- rule two: do not touch prod\n", "- rule two: touch it\n"),
                         ("- rule two: merge only with a yes\n", "- rule two: take it in with a yes\n")):
            with self.subTest(old=old):
                src = FULL.replace("- rule two\n", old)
                self.pair(src=src)
                self.write("CLAUDE.md", src.replace(old, new))
                cur = src.replace(old, new)
                self.assertTrue(self.prompt().startswith(cur.rstrip("\n")))
                self.assertEqual((self.used()["kind"], self.used()["stale"], self.used()["why"]), ("full", "changed", "gate"))

    def test_a_gate_word_of_the_language_data_counts_too(self):
        from core import langdata
        word = next(w for frag in langdata.words("words", "gate") for w in frag.split("|") if re.fullmatch(r"\w{4,}", w))
        old = f"- rule two: {word} push\n"
        src = FULL.replace("- rule two\n", old)
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace(old, "- rule two: push\n"))
        self.prompt()
        self.assertEqual((self.used()["kind"], self.used()["why"]), ("full", "gate"))

    def test_a_line_replaced_by_an_empty_one_is_a_removal(self):
        self.pair()
        self.write("CLAUDE.md", FULL.replace("- lesson one\n", "\n"))
        self.assertNotIn(execprofile.DELTA_HEAD, self.prompt())
        self.assertEqual((self.used()["kind"], self.used()["why"]), ("full", "removed"))

    def test_an_item_deleted_next_to_an_edited_one_is_a_removal(self):
        src = FULL.replace("- rule two\n", "- rule two: run make test\n- you may skip the slow suite on docs\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("- rule two: run make test\n- you may skip the slow suite on docs\n",
                                            "- rule two: run make test, all suites\n"))
        self.prompt()
        self.assertEqual((self.used()["kind"], self.used()["why"]), ("full", "removed"))

    def test_the_delta_carries_the_whole_item_of_a_changed_line(self):
        src = FULL.replace("- rule two\n", "- rule two: keep branches short;\n  never\n  push to main directly\n  on a Friday\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("  push to main directly\n", "  push to main or release directly\n"))
        delta = self.prompt().split(execprofile.DELTA_HEAD, 1)[1]
        self.assertIn("- rule two: keep branches short;\n  never\n  push to main or release directly\n  on a Friday", delta)

    def test_a_negation_split_over_two_lines_counts_when_its_first_line_changes(self):
        src = FULL.replace("- rule two\n", "- rule two: you must not\n  push there directly\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("- rule two: you must not\n", "- rule two: you must ask, then\n"))
        self.prompt()
        self.assertEqual((self.used()["kind"], self.used()["why"]), ("full", "gate"))

    def test_a_typographic_apostrophe_is_a_negation_too(self):
        src = FULL.replace("- rule two\n", "- rule two: don\u2019t push to main\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("- rule two: don\u2019t push to main\n", "- rule two: push to main\n"))
        self.prompt()
        self.assertEqual((self.used()["kind"], self.used()["why"]), ("full", "gate"))

    def test_a_wrapped_paragraph_without_indent_is_one_item(self):
        src = FULL.replace("- rule two\n", "Never deploy on Fridays; deploys go through staging and\nthe owner checks them.\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("the owner checks them.", "the owner checks them twice."))
        delta = self.prompt().split(execprofile.DELTA_HEAD, 1)[1]
        self.assertIn("Never deploy on Fridays; deploys go through staging and\nthe owner checks them twice.", delta)

    def test_an_item_replaced_by_an_unrelated_one_is_a_removal(self):
        src = FULL.replace("- rule two\n", "- rule two: run make test\n- you may skip the slow suite on docs\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("- rule two: run make test\n- you may skip the slow suite on docs\n",
                                            "- rule two: run make test, all suites\n- rule three: lint the diff\n"))
        self.prompt()
        self.assertEqual((self.used()["kind"], self.used()["why"]), ("full", "removed"))

    def test_the_indented_paragraph_of_a_loose_item_belongs_to_it(self):
        src = FULL.replace("- rule two\n", "- rule two: never push to main.\n\n  Ask the owner first.\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("  Ask the owner first.", "  Ask the owner first, in writing."))
        delta = self.prompt().split(execprofile.DELTA_HEAD, 1)[1]
        self.assertIn("- rule two: never push to main.\n\n  Ask the owner first, in writing.", delta)

    def test_an_indented_code_block_changed_gives_the_full_profile(self):
        src = FULL.replace("- rule two\n", "- rule two: run\n\n    ```\n    make\n\n    make x\n    ```\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("make x", "make y"))
        self.assertNotIn(execprofile.DELTA_HEAD, self.prompt())
        self.assertEqual(self.used()["why"], "code")

    def test_a_block_fenced_with_tildes_keeps_backticks_inside(self):
        src = FULL.replace("## Lessons\n", "## Example\n~~~md\n```sh\nmake\n```\n~~~\n\n## Lessons\n")
        self.pair(src=src)
        self.write("CLAUDE.md", src.replace("- lesson two\n", "- lesson two\n- lesson three\n"))
        delta = self.prompt().split(execprofile.DELTA_HEAD, 1)[1]
        self.assertIn('(in the section "## Lessons")\n- lesson three', delta)
        self.assertEqual(self.used()["kind"], "executor+delta")

    def test_a_delta_that_makes_it_no_shorter_than_the_full_profile_gives_the_full_one(self):
        self.pair(short="# Agent (digest)\n\n" + "- rule one, said at length " + "x" * len(FULL) + "\n")
        self.write("CLAUDE.md", FULL.replace("- lesson two\n", "- lesson two\n- lesson three\n"))
        self.assertNotIn(execprofile.DELTA_HEAD, self.prompt())
        self.assertEqual((self.used()["kind"], self.used()["stale"]), ("full", "delta"))

    def test_removed_empty_lines_change_nothing(self):
        self.pair()
        self.write("CLAUDE.md", FULL.replace("\n\n## Lessons", "\n## Lessons"))
        self.assertTrue(self.prompt().startswith(SHORT.rstrip("\n") + "\n\n"))
        self.assertEqual(self.used()["kind"], "executor")

    def test_a_removed_line_gives_the_full_profile_and_marks_stale(self):
        self.pair()
        self.write("CLAUDE.md", FULL.replace("- lesson one\n", ""))
        self.assertNotIn("digest", self.prompt())
        self.assertEqual((self.used()["kind"], self.used()["stale"], self.used()["why"]), ("full", "changed", "removed"))

    def test_a_large_addition_gives_the_full_profile_and_marks_stale(self):
        self.pair()
        self.write("CLAUDE.md", FULL + "".join(f"- new {i} " + "x" * 100 + "\n" for i in range(600)))
        self.assertNotIn("digest", self.prompt())
        self.assertEqual((self.used()["kind"], self.used()["stale"]), ("full", "delta"))

    def test_a_pair_whose_hash_does_not_match_gives_the_full_profile(self):
        self.pair(sha="0" * 64)
        self.assertTrue(self.prompt().startswith(FULL.rstrip("\n")))
        self.assertEqual((self.used()["kind"], self.used()["stale"]), ("full", "pair"))

    def test_half_a_pair_gives_the_full_profile(self):
        self.pair()
        os.unlink(os.path.join(self.dir, "CLAUDE.executor.src.md"))
        self.assertTrue(self.prompt().startswith(FULL.rstrip("\n")))
        self.assertEqual(self.used()["stale"], "pair")

    def test_the_agent_itself_always_gets_the_full_profile(self):
        self.pair()
        text = self.prompt(executor_of=None)
        self.assertTrue(text.startswith(FULL.rstrip("\n")))
        self.assertIsNone(self.used())

    def test_a_record_of_the_last_launch_does_not_stick_to_the_next(self):
        self.pair()
        self.prompt()
        self.prompt(executor_of=None)
        self.assertIsNone(self.used())


    def test_a_trailing_blank_line_in_the_source_does_not_hide_an_addition(self):
        self.pair(src=FULL + "\n")
        self.write("CLAUDE.md", FULL.replace("- rule two\n", "- rule two\n- lesson three\n") + "\n")
        self.assertIn("- lesson three", self.prompt())
        self.assertEqual(self.used()["kind"], "executor+delta")

    def test_an_addition_among_repeated_lines_is_still_an_addition(self):
        src = BACK + "\n" + "- c\n" * 5
        self.pair(src=src)
        self.write("CLAUDE.md", BACK + "\n" + "- c\n- c\n- new\n- c\n- c\n- c\n")
        self.assertIn("- new", self.prompt())
        self.assertEqual(self.used()["kind"], "executor+delta")

    def test_the_section_of_an_addition_is_a_markdown_heading_not_a_comment_in_code(self):
        src = "# A\n\n" + BACK + "\n## Build\n```sh\n# install dependencies\nmake\n```\n"
        self.pair(src=src)
        self.write("CLAUDE.md", src + "- new\n")
        text = self.prompt()
        self.assertIn("## Build", text.split("- new")[0].rsplit("(", 1)[1])
        self.assertNotIn("install dependencies»", text)

    def test_an_addition_inside_or_with_a_code_block_gives_the_full_profile(self):
        src = "# A\n\n## Build\n```sh\nmake\n```\n"
        for cur in (src.replace("make\n", "make\nrm -rf build\n"), src + "\n```\nnew\n```\n", src.replace("make\n", "make all\n")):
            self.pair(src=src)
            self.write("CLAUDE.md", cur)
            self.assertNotIn("digest", self.prompt())
            self.assertEqual((self.used()["stale"], self.used()["why"]), ("changed", "code"))

    def test_a_new_section_comes_with_its_own_heading(self):
        self.pair()
        self.write("CLAUDE.md", FULL + "\n## Deploy\n- step\n")
        text = self.prompt()
        self.assertIn("\n\n## Deploy\n- step", text)
        self.assertNotIn("«## Lessons»", text)

    def test_a_short_profile_with_only_its_header_gives_the_full_one(self):
        self.pair(short="\n")
        self.assertTrue(self.prompt().startswith(FULL.rstrip("\n")))
        self.assertEqual(self.used()["stale"], "pair")

    def test_a_resume_with_the_first_prompt_records_nothing(self):
        self.pair()
        self.host.command("w", "s-1", resume=True, agent_file=self.file, executor_of="maps")
        self.assertIsNone(self.used())

    def test_a_module_sees_the_short_profile_as_the_agent_file_part(self):
        from tests.test_modapi_prompt import Stub
        mods = Stub(m=lambda p, d: None)
        self.host.modules = mods
        self.pair()
        self.host.command("w", "s-1", agent_file=self.file, executor_of="maps")
        self.assertEqual(mods.calls[0][2]["parts"][0], {"name": "agent_file", "text": SHORT.rstrip("\n")})


class Pick(unittest.TestCase):
    def test_no_agent_file_no_pair(self):
        self.assertIsNone(execprofile.pick(os.path.join(tempfile.mkdtemp(), "CLAUDE.md")))


class Journal(unittest.TestCase):
    def test_the_spawn_fact_carries_which_profile_went(self):
        from core.daemon import Hub
        info = {"kind": "full", "stale": "changed", "bytes": 10, "full_bytes": 10, "source": "ab", "pair": "cd"}
        host = types.SimpleNamespace(profile_used={})

        def spawn(name, **_):
            host.profile_used[name] = info
            return "spawned"
        host.spawn = spawn
        h = Hub(tempfile.mkdtemp(prefix="execprof-d-"), host)
        own = {"channel": "cli", "owner": True}
        h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **own})
        h.execute({"do": "spawn", "session": "maps", "session_id": "s-m", "cwd": "/m"})
        from core import journal
        done = [e for e in journal.read(h.journal.path) if e["type"] == "host.done"][-1]
        self.assertEqual(done["data"].get("profile"), info)
        self.assertEqual(host.profile_used, {})


if __name__ == "__main__":
    unittest.main()
