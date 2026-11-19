"""Which skills and MCP tools a session really used (spec d2547143, core/uses.py). The owner was sure the sessions used
all their skills, and they did not: the page now counts the uses from the journal. Here: an archive and the live
journal made of the facts the hosts write — a Skill step, a `/skill` that opened a turn (the owner's or `hub slash`), a
read of a skill's SKILL.md (Read, or Codex's `bash -lc 'cat …'`), an MCP step — with what rotation and the core do to
them: the tail carried into the next journal, a call and its result in different journals, a rejected fact."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import tempfile
import unittest

from core import uses

DAY = 86400
NOW = 1_800_000_000.0
PLUGIN = "/home/x/plug/geo/skills/tiles/SKILL.md"
CODEX_SKILL = "/home/x/.agents/skills/geo-tiles/SKILL.md"
KIT = {"engine": "claude", "skills": [{"name": "geo:tiles", "plugin": "geo", "path": PLUGIN, "in_prompt": True},
                                      {"name": "geo-tiles", "path": CODEX_SKILL},
                                      {"name": "release-notes", "source": "repo", "in_prompt": False}],
       "plugin_errors": [], "mcp": [{"name": "my.atlas", "tools": 9}]}


class Journal:
    def __init__(self):
        self.events = []

    def ev(self, seq, ts, type, entity, **data):
        self.events.append({"seq": seq, "ts": ts, "src": "host", "type": type, "entity": entity, "data": data})
        return self

    def tool(self, seq, ts, s, tid, name, inp, **more):
        return self.ev(seq, ts, "turn.tool", s, id=tid, name=name, input=inp, **more)

    def res(self, seq, ts, s, tid, error=False, text="ok"):
        return self.ev(seq, ts, "turn.tool_result", s, id=tid, error=error, text=text)

    def write(self, path):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in self.events))


class Uses(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uses-")
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        t0 = NOW - 40 * DAY          # before the update: no set yet
        t1 = NOW - 3 * DAY           # this week
        old = (Journal()
               .ev(1, t0, "session.started", "maps", session_id="c-old", source="startup")
               .ev(2, t0, "session.info", "maps", builtins=["compact", "model"])
               .tool(3, t0, "maps", "a1", "Skill", {"skill": "geo:tiles"}).res(4, t0, "maps", "a1")
               .ev(5, t0, "turn.started", "maps", prompt_head="/maps-work ABC-123 go")     # a scaffold before any set
               .ev(6, t0, "turn.started", "maps", prompt_head="/compact")             # built-in: not a skill
               .ev(7, t1, "session.started", "maps", session_id="c-now", source="clear")
               .ev(8, t1, "session.kit", "maps", **KIT)
               .tool(9, t1, "maps", "b1", "Skill", {"skill": "tiles"})                # short name, one plugin has it
               .res(10, t1, "maps", "b1")
               .tool(11, t1, "maps", "b2", "Skill", {"skill": "geo:tiles"})
               .res(12, t1, "maps", "b2", error=True, text="Unknown skill")
               .tool(13, t1, "maps", "b3", "mcp__my_atlas__skill_reference", {"collection": "docs", "name": "atlas-tiles"})
               .res(14, t1, "maps", "b3")
               .tool(15, t1, "maps", "b4", "mcp__my_atlas__skill_reference", {"name": "atlas-tiles", "collection": "docs"})
               .tool(16, t1, "maps", "b5", "mcp__my_atlas__search", {"query": "tiles near river"})
               .res(17, t1, "maps", "b5")
               .tool(18, t1, "maps", "b6", "Bash", {"command": f"/bin/bash -lc 'cd /home/x/maps && cat {CODEX_SKILL}'"})
               .res(19, t1, "maps", "b6", text="---\nname: geo-tiles")
               .tool(20, t1, "maps", "b7", "Bash", {"command": f"echo {CODEX_SKILL}"}).res(21, t1, "maps", "b7")
               .tool(22, t1, "maps", "b8", "Read", {"file_path": PLUGIN}).res(23, t1, "maps", "b8", error=True, text="no")
               .ev(24, t1, "turn.started", "maps", prompt_head="/geo:tiles redo the layer")
               .ev(25, t1, "turn.started", "maps", prompt_head="/nothing-like-it")
               .tool(26, t1, "maps", "b9", "Skill", {"skill": "geo:tiles"}, parent="toolu_x")  # its helper agent
               .res(27, t1, "maps", "b9"))
        old.write(os.path.join(self.dir, "journal-archive", "journal-1-27.jsonl"))
        live = (Journal()
                .ev(1, t0, "core.snapshot", None, why="rotate-start")
                .tool(26, t1, "maps", "b9", "Skill", {"skill": "geo:tiles"}, parent="toolu_x")   # the carried tail
                .res(27, t1, "maps", "b9")
                .tool(15, t1, "maps", "b4", "mcp__my_atlas__skill_reference", {"name": "atlas-tiles"})   # carried, open
                .res(28, t1 + 5, "maps", "b4")                                    # its result comes in the new journal
                .tool(29, t1 + 6, "maps", "c1", "Skill", {"skill": "release-notes"})
                .ev(30, t1 + 6, "core.rejected", None, of=29)                     # the core did not take it
                .ev(35, t1 + 6, "turn.started", "maps-3", prompt_head="redo the layer")   # the executor's only turn
                .tool(31, t1 + 7, "maps-3", "d1", "Skill", {"skill": "geo:tiles"}).res(32, t1 + 7, "maps-3", "d1")
                .tool(33, t1 + 8, "old-name", "e1", "Skill", {"skill": "geo:tiles"}).res(34, t1 + 8, "old-name", "e1"))
        live.write(os.path.join(self.dir, "journal.jsonl"))
        self.reader = uses.Reader(self.dir)
        self.reader.build_here()
        lead_kit = {**KIT, "skills": KIT["skills"] + [{"name": "tiles-kit:standards", "source": "plugin"},
                                                      {"name": "code-review", "source": "built-in"}]}
        self.st = {"sessions": {
            "maps": {"state": "idle", "was": ["old-name"], "kit": {**KIT, "at": t1}, "session_id": "c-now",
                     "raised_by": "lead", "info": {"mcp": ["my.atlas", "search"], "mcp_auth": ["search"],
                                                   "mcp_connected": ["my.atlas"]}},
            "lead": {"state": "idle", "kit": lead_kit},
            "maps-3": {"state": "removed", "raised_by": "maps"}}}

    def report(self, days=7):
        return uses.report(self.reader, self.st, "maps", days, NOW, background=False)

    def test_what_the_session_used_this_week(self):
        got = self.report()
        own = got["used"]["own"]
        tiles = own["skill:geo:tiles"]
        # b1 (short name) + b2 (failed) + b9 (helper; carried tail once) + e1 under the former name; the command once;
        # the failed Read is not a use
        self.assertEqual((tiles["tool"], tiles["failed"], tiles["command"], tiles["file"]), (4, 1, 1, 0))
        self.assertTrue(tiles["this_conversation"])
        self.assertEqual(own["skill:geo-tiles"]["file"], 1)                 # Codex's read through bash -lc
        self.assertNotIn("skill:release-notes", own)                        # rejected by the core
        self.assertNotIn("skill:nothing-like-it", own)                      # a command the set does not have
        ref = own["mcp:my.atlas/skill_reference"]                           # the server's name as the set has it
        self.assertEqual((ref["tool"], ref["unknown"], ref["args"]), (2, 0, {"name=atlas-tiles": 2}))
        self.assertNotIn("args", own["mcp:my.atlas/search"])                # free text is not split
        work = own["skill:maps-work"]                                         # 40 days ago: none this week, but its
        self.assertEqual((work["command"], work["last"]), (0, NOW - 40 * DAY))   # last use shows
        self.assertEqual(got["used"]["executors"]["skill:geo:tiles"]["tool"], 1)
        self.assertEqual(got["used"]["executors_count"], 1)
        self.assertEqual(got["mcp_status"], {"my.atlas": "connected", "search": "needs-auth"})
        self.assertEqual(got["lead"], {"name": "lead", "missing": ["tiles-kit:standards"]})
        self.assertTrue(got["complete"] and got["period_ready"])

    def test_all_the_time_and_the_scaffold_before_the_update(self):
        own = self.report(days=0)["used"]["own"]
        work = own["skill:maps-work"]                                         # …but counted over all the time
        self.assertEqual((work["command"], work["unverified"]), (1, True))
        self.assertNotIn("skill:compact", own)
        self.assertEqual(own["skill:geo:tiles"]["tool"], 5)                 # with the one before the update
        self.assertEqual(own["skill:geo:tiles"]["last"], NOW - 3 * DAY + 8)

    def test_a_skill_named_through_a_sibling_working_copy_is_the_same_skill(self):
        """Claude Code names a skill of the parent repo, seen from a working copy, `.claude/worktrees/<copy>:<skill>`
        (the set strips it, core/kit.name_of): its calls are the skill's, not a row of their own."""
        d = tempfile.mkdtemp(prefix="uses-wt-")
        os.makedirs(os.path.join(d, "journal-archive"))
        t = NOW - DAY
        (Journal().ev(1, t, "session.started", "maps", session_id="c1", source="startup")
         .ev(2, t, "session.kit", "maps", **KIT)
         .tool(3, t, "maps", "w1", "Skill", {"skill": ".claude/worktrees/feat+ABC-123-x:release-notes"}).res(4, t, "maps", "w1")
         .tool(5, t, "maps", "w2", "Skill", {"skill": "release-notes"}).res(6, t, "maps", "w2")
         .write(os.path.join(d, "journal.jsonl")))
        reader = uses.Reader(d)
        got = uses.report(reader, {"sessions": {"maps": {"state": "idle", "kit": {**KIT, "at": t}, "session_id": "c1"}}},
                          "maps", 7, NOW, background=False)
        own = got["used"]["own"]
        self.assertEqual([k for k in own if "release-notes" in k], ["skill:release-notes"])
        self.assertEqual(own["skill:release-notes"]["tool"], 2)

    def test_a_digest_is_read_once_and_kept_next_to_the_archive(self):
        side = os.path.join(self.dir, "journal-archive", "journal-1-27.jsonl" + uses.SIDE)
        self.assertTrue(os.path.isfile(side))
        again = uses.Reader(self.dir)
        digests, complete, _ = again.digests_now(background=False)
        self.assertTrue(complete)
        with open(side, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["sums"], digests[0]["sums"])

    def test_an_undigested_archive_says_so_and_the_live_journal_still_counts(self):
        os.remove(os.path.join(self.dir, "journal-archive", "journal-1-27.jsonl" + uses.SIDE))
        got = uses.report(uses.Reader(self.dir), self.st, "maps", 7, NOW, background=False)
        self.assertFalse(got["complete"])
        self.assertEqual(got["used"]["own"]["skill:geo:tiles"]["tool"], 1)   # e1 of the live journal

    def test_the_denominators_of_a_period(self):
        """"Opened in 1 conversation of 3" needs the 3: the conversations that had a turn in the period, and the turns."""
        got = self.report()
        self.assertEqual(got["volume"], {"own": {"conversations": 1, "turns": 2}, "executors": {"conversations": 1, "turns": 1}})
        everything = self.report(days=0)["volume"]["own"]
        self.assertEqual(everything, {"conversations": 2, "turns": 4})      # c-old (40 days ago) and c-now
        tiles = got["used"]["own"]["skill:geo:tiles"]
        self.assertEqual(tiles["convs"], 1)                                 # all its uses of the week are c-now's
        self.assertEqual(self.report(days=0)["used"]["own"]["skill:geo:tiles"]["convs"], 2)
        self.assertEqual(got["used"]["own"]["skill:maps-work"]["convs"], 0)  # 40 days ago: none in the period
        self.assertEqual(self.report(days=0)["used"]["own"]["skill:maps-work"]["convs"], 1)

    def test_a_digest_made_before_the_turns_were_kept_is_made_again(self):
        side = os.path.join(self.dir, "journal-archive", "journal-1-27.jsonl" + uses.SIDE)
        with open(side, encoding="utf-8") as f:
            digest = json.load(f)
        self.assertTrue(digest["turns"])
        del digest["turns"]                                                 # what an earlier core wrote
        with open(side, "w", encoding="utf-8") as f:
            json.dump(digest, f)
        got = uses.report(uses.Reader(self.dir), self.st, "maps", 7, NOW, background=False)
        self.assertFalse(got["complete"])                                   # not trusted: the denominators would be short
        again = uses.Reader(self.dir)
        again.build_here()
        self.assertTrue(uses.report(again, self.st, "maps", 7, NOW, background=False)["complete"])

    def test_the_whole_hub_at_once(self):
        got = uses.fleet(self.reader, self.st, 7, NOW, background=False)
        self.assertEqual((got["complete"], got["period_ready"]), (True, True))
        self.assertEqual(got["sessions"], {"with_set": 2, "active": 2})     # maps and lead have a set; maps and maps-3 had a turn
        self.assertEqual(got["volume"], {"conversations": 2, "turns": 3})
        rows = {r["name"]: r for r in got["skills"]}
        tiles = rows["geo:tiles"]
        # maps: b1 b2(failed) b9 e1 (the carried tail once) + command; the executor maps-3: d1
        self.assertEqual((tiles["loaded"], tiles["in_prompt"], tiles["tool"], tiles["command"], tiles["failed"]), (2, 2, 5, 1, 1))
        self.assertEqual((tiles["used_by"], tiles["convs"], tiles["calls"]), (2, 2, 6))
        self.assertEqual((rows["code-review"]["loaded"], rows["code-review"]["calls"]), (1, 0))   # loaded by lead only, never opened
        self.assertEqual(rows["tiles-kit:standards"]["used_by"], 0)
        self.assertEqual(got["skills"][0]["name"], "geo:tiles")             # the most used first
        atlas = {r["name"]: r for r in got["mcp"]}["my.atlas"]
        self.assertEqual((atlas["loaded"], atlas["tools"], atlas["tool"], atlas["used_by"]), (2, 9, 3, 1))   # the server's own name; its two tools of one session: one user

    def test_the_list_s_tokens_are_summed_over_the_sessions_that_say_them(self):
        st = {"sessions": {n: {**s, "kit": {**s["kit"], "listed": {"in_prompt": 3, "total": 4, "tokens": tok}}}
                           if n == "maps" else s for n, s in self.st["sessions"].items() for tok in [120]}}
        listed = uses.fleet(self.reader, st, 7, NOW, background=False)["listed"]
        self.assertEqual(listed, {"known": 1, "tokens": 120})              # lead's (older record) says none

    def test_read_paths(self):
        self.assertEqual(uses.read_paths("sed -n '1,80p' /a/skills/x/SKILL.md | head"), ["/a/skills/x/SKILL.md"])
        self.assertEqual(uses.read_paths("echo /a/skills/x/SKILL.md; ls /a"), [])
        self.assertEqual(uses.read_paths("zsh -c \"cd /a && cat /a/skills/y/SKILL.md\""), ["/a/skills/y/SKILL.md"])


if __name__ == "__main__":
    unittest.main()


class Edges(unittest.TestCase):
    """What a code review found (17.11): a conversation, a set and a result across journals; a rename; a reused name;
    a tab in a name; a scrubbed archive."""
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uses-edge-")
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        self.t = NOW - 2 * DAY

    def write(self, archive, live):
        archive.write(os.path.join(self.dir, "journal-archive", "journal-1-10.jsonl"))
        live.write(os.path.join(self.dir, "journal.jsonl"))
        self.reader = uses.Reader(self.dir)
        self.reader.build_here()

    def report(self, st, name="maps", days=7):
        return uses.report(self.reader, st, name, days, NOW, background=False)["used"]["own"]

    def test_a_conversation_and_a_set_carry_into_the_next_journal_and_a_rename(self):
        t = self.t
        self.write(Journal().ev(1, t, "session.started", "old-name", session_id="c1", source="startup")
                   .ev(2, t, "session.kit", "old-name", **KIT)
                   .tool(3, t, "old-name", "o1", "mcp__my_atlas__render", {"q": "a b"}),   # open at rotation, not carried
                   Journal().ev(11, t + 60, "turn.tool_result", "maps", id="o1", error=False, text="ok")
                   .tool(12, t + 70, "maps", "n1", "Read", {"file_path": PLUGIN}).res(13, t + 70, "maps", "n1", text="x")
                   .tool(14, t + 80, "maps", "n2", "Skill", {"skill": "a\tb"}).res(15, t + 80, "maps", "n2"))
        st = {"sessions": {"maps": {"state": "idle", "was": ["old-name"], "session_id": "c1", "kit": KIT}}}
        own = self.report(st)
        self.assertEqual(own["skill:geo:tiles"]["file"], 1)              # the set of 'old-name' reads the path under 'maps'
        self.assertTrue(own["skill:geo:tiles"]["this_conversation"])     # the conversation carried in from the archive
        render = own["mcp:my.atlas/render"]                              # the set's server name, not the tool's
        self.assertEqual((render["tool"], render["unknown"]), (1, 0))   # its result came in the next journal
        self.assertIn("skill:a b", own)                                  # a tab in a name does not break the report

    def test_a_reused_name_does_not_inherit_the_earlier_sessions_uses(self):
        old = NOW - 40 * DAY
        self.write(Journal().ev(1, old, "session.requested", "fix-x")
                   .tool(2, old, "fix-x", "x1", "Skill", {"skill": "geo:tiles"}).res(3, old, "fix-x", "x1")
                   .ev(4, old + DAY, "session.remove_requested", "fix-x"),
                   Journal().ev(11, self.t, "session.requested", "fix-x")
                   .tool(12, self.t, "fix-x", "x2", "Skill", {"skill": "release-notes"}).res(13, self.t, "fix-x", "x2"))
        own = self.report({"sessions": {"fix-x": {"state": "idle"}}}, "fix-x", days=0)
        self.assertEqual(sorted(own), ["skill:release-notes"])

    def test_a_woken_session_keeps_its_history(self):
        # session.requested also wakes a sleeping session: it is the same session
        old = NOW - 20 * DAY
        self.write(Journal().ev(1, old, "session.requested", "maps")
                   .tool(2, old, "maps", "w1", "Skill", {"skill": "geo:tiles"}).res(3, old, "maps", "w1"),
                   Journal().ev(11, self.t, "session.requested", "maps"))
        own = self.report({"sessions": {"maps": {"state": "idle"}}}, days=0)
        self.assertEqual(own["skill:geo:tiles"]["tool"], 1)

    def test_a_former_name_taken_by_another_session(self):
        t0, t1 = NOW - 10 * DAY, NOW - 2 * DAY
        self.write(Journal().tool(1, t0, "old-name", "a1", "Skill", {"skill": "geo:tiles"}).res(2, t0, "old-name", "a1")
                   .ev(3, t0 + DAY, "session.renamed", "old-name", to="maps"),
                   Journal().ev(11, t1, "session.requested", "old-name")
                   .tool(12, t1, "old-name", "b1", "Skill", {"skill": "release-notes"}).res(13, t1, "old-name", "b1"))
        st = {"sessions": {"maps": {"state": "idle", "was": ["old-name"]}, "old-name": {"state": "idle"}}}
        self.assertEqual(sorted(self.report(st, days=0)), ["skill:geo:tiles"])
        self.assertEqual(sorted(self.report(st, "old-name", days=0)), ["skill:release-notes"])

    def test_a_failed_read_across_journals_is_not_a_use(self):
        t = self.t
        self.write(Journal().ev(1, t, "session.kit", "maps", **KIT).tool(2, t, "maps", "r1", "Read", {"file_path": PLUGIN}),
                   Journal().ev(11, t + 5, "turn.tool_result", "maps", id="r1", error=True, text="denied"))
        self.assertEqual(self.report({"sessions": {"maps": {"state": "idle"}}}), {})

    def test_a_scrubbed_archive_is_read_again(self):
        self.write(Journal().tool(1, self.t, "maps", "s1", "Skill", {"skill": "geo:tiles"}).res(2, self.t, "maps", "s1"),
                   Journal())
        st = {"sessions": {"maps": {"state": "idle"}}}
        self.assertIn("skill:geo:tiles", self.report(st))
        archive = os.path.join(self.dir, "journal-archive", "journal-1-10.jsonl")
        Journal().tool(1, self.t, "maps", "s1", "Skill", {"skill": "geo:styles"}).write(archive)   # rewritten in place
        os.remove(archive + uses.SIDE)                                    # scrub drops the file next to it
        got = uses.report(self.reader, st, "maps", 7, NOW, background=False)
        self.assertFalse(got["complete"])                                 # the old digest is forgotten
        self.assertNotIn("skill:geo:tiles", got["used"]["own"])


class Cli(unittest.TestCase):
    """`hub session kit`: the same answer as the page's, as a compact text or --json (core/kitcli.py)."""
    HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")

    def setUp(self):
        from core import journal
        self.state = tempfile.mkdtemp(prefix="kitcli-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        kit = {**KIT, "listed": {"in_prompt": 2, "total": 3, "tokens": 120}, "engine": "claude",
               "skills": [{**r, "tokens": n} for r, n in zip(KIT["skills"], (80, 30, 10))]}
        for name, raised in (("maps", None), ("maps-3", "maps")):
            j.append("session.requested", name, {"session_id": "s-" + name, "cwd": "/w", "channel": "web", "owner": True,
                                                 **({"executor": True, "raised_by": raised} if raised else {})})
            j.append("session.started", name, {"session_id": "s-" + name, "source": "startup"})
            j.append("session.kit", name, kit)
        for n, (name, who) in enumerate([("geo:tiles", "maps"), ("geo:tiles", "maps"), ("geo:tiles", "maps-3")]):
            j.append("turn.started", who, {"prompt_head": "redo the layer"})
            j.append("turn.tool", who, {"id": f"t{n}", "name": "Skill", "input": {"skill": name}})
            j.append("turn.tool_result", who, {"id": f"t{n}", "error": False, "text": "ok"})
        j.append("turn.tool", "maps", {"id": "m1", "name": "mcp__my_atlas__search", "input": {"query": "river"}})
        j.append("turn.tool_result", "maps", {"id": "m1", "error": False, "text": "ok"})

    def run_hub(self, *args, session="maps"):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "PYTHONUTF8": "1", "HUB_LANG": "en"}
        env.pop("HUB_SESSION", None)
        if session:
            env["HUB_SESSION"] = session
        import subprocess
        import sys
        return subprocess.run([sys.executable, self.HUB, "session", "kit", *args], env=env, capture_output=True,
                              text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=120)

    def test_a_session_as_text(self):
        r = self.run_hub()                                                  # no name: itself
        self.assertEqual(r.returncode, 0, r.stderr)
        text = r.stdout
        self.assertIn("maps — 7 days; the set as of", text)
        self.assertIn("Skills in the set: 3; in the model's list: 2; 120 tokens of the context", text)
        self.assertIn("In the period: conversations 1, turns 2; executors: conversations 1, turns 1", text)
        self.assertIn("Opened — 1 of 3", text)
        row = next(l for l in text.splitlines() if l.lstrip().startswith("geo:tiles"))
        self.assertEqual(row.split()[:4], ["geo:tiles", "2", "1", "1/1"])    # itself, its executor, conversations of 1, then last
        self.assertIn("80", row)                                            # its tokens
        self.assertIn("Never opened — 2, 40 tokens of the context: geo-tiles (30), release-notes (10)", text)
        self.assertIn("my.atlas", text)

    def test_a_set_that_says_no_tokens_shows_dashes_not_zeros(self):
        from core import journal
        bare = {**KIT, "listed": {"in_prompt": 2, "total": 3, "tokens": None}, "engine": "claude"}   # an older Claude Code
        journal.Journal(os.path.join(self.state, "journal.jsonl")).append("session.kit", "maps", bare)
        text = self.run_hub().stdout
        self.assertIn("Skills in the set: 3; in the model's list: 2\n", text)
        self.assertIn("Never opened — 2: ", text)                         # no ", 0 tokens"
        self.assertNotIn("tokens", text.split("Opened")[0])                  # nothing said before the table
        self.assertTrue(next(l for l in text.splitlines() if l.lstrip().startswith("geo:tiles")).rstrip().endswith("—"))

    def test_json_is_the_answer_the_page_gets(self):
        got = json.loads(self.run_hub("maps-3", "--json", "--days", "all").stdout)
        self.assertEqual((got["session"], got["days"]), ("maps-3", 0))
        self.assertEqual(got["used"]["own"]["skill:geo:tiles"]["tool"], 1)
        self.assertEqual(got["volume"]["own"], {"conversations": 1, "turns": 1})
        self.assertEqual(got["kit"]["listed"], {"in_prompt": 2, "total": 3, "tokens": 120})

    def test_the_whole_hub(self):
        r = self.run_hub("--fleet", session=None)                           # from a terminal, no name needed
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("7 days: sessions with a set now — 2; sessions that had turns — 2", r.stdout)
        self.assertIn("The skill list in the model's context: 120 tokens on average, by 2 sessions", r.stdout)
        row = next(l for l in r.stdout.splitlines() if l.lstrip().startswith("geo:tiles"))
        self.assertEqual(row.split()[:5], ["geo:tiles", "2", "3", "2", "2"])   # loaded in 2, 3 calls, 2 sessions, 2 conversations
        self.assertIn("Loaded and never opened — 2, 80 tokens of the context in all sessions together: "
                      "geo-tiles ×2, release-notes ×2", r.stdout)         # 40 tokens in each of two lists
        got = json.loads(self.run_hub("--fleet", "--json", session=None).stdout)
        self.assertEqual(got["skills"][0]["name"], "geo:tiles")

    def test_wrong_calls_say_how_to_call(self):
        self.assertNotEqual(self.run_hub(session=None).returncode, 0)       # a terminal without a name or --fleet
        self.assertIn("no session nope", self.run_hub("nope").stderr)
        for args in (("--days", "5"), ("maps", "maps-3"), ("--fleet", "maps"), ("--when",), ("--days", "7", "--days", "30"),
                     ("--jsn",)):
            r = self.run_hub(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("hub session kit", r.stderr + r.stdout, args)

    def test_an_archive_without_a_digest_is_digested_by_the_command_itself(self):
        archive = os.path.join(self.state, "journal-archive", "journal-1-5.jsonl")
        os.makedirs(os.path.dirname(archive))
        Journal().tool(1, NOW - DAY, "maps", "o1", "Skill", {"skill": "release-notes"}).res(2, NOW - DAY, "maps", "o1").write(archive)
        r = self.run_hub("--json")
        self.assertTrue(os.path.isfile(archive + uses.SIDE))
        got = json.loads(r.stdout)
        self.assertTrue(got["complete"])
        self.assertEqual(got["used"]["own"]["skill:release-notes"]["tool"], 1)


class Page(unittest.TestCase):
    """GET /api/session/kit on the page server: the set from the core state, the uses from the journal."""
    def setUp(self):
        import threading
        from core import journal
        from modules.web.server import Web
        self.state = tempfile.mkdtemp(prefix="usesweb-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "maps", {"session_id": "s", "cwd": "/w", "channel": "web", "owner": True})
        j.append("session.started", "maps", {"session_id": "s", "source": "startup"})
        j.append("session.kit", "maps", KIT)
        j.append("turn.tool", "maps", {"id": "t1", "name": "Skill", "input": {"skill": "geo:tiles"}})
        j.append("turn.tool_result", "maps", {"id": "t1", "error": False, "text": "ok"})
        self.web = Web(self.state)
        self.server = self.web.serve(port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.web.tail.poll()

    def tearDown(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        import http.client
        port = self.server.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, json.loads(body or b"null")

    def test_a_sessions_set_and_uses(self):
        code, got = self.get("/api/session/kit?session=maps&days=30")
        self.assertEqual(code, 200)
        self.assertEqual(got["days"], 30)
        self.assertEqual([r["name"] for r in got["kit"]["skills"]], ["geo:tiles", "geo-tiles", "release-notes"])
        self.assertEqual(got["used"]["own"]["skill:geo:tiles"]["tool"], 1)
        self.assertEqual(self.get("/api/session/kit?session=nope")[0], 404)
