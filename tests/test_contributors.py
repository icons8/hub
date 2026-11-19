"""tools/contributors.py: the contributors of README.md and CONTRIBUTORS.md and the co-authors of a release commit, from
the thanks of CHANGES.md (owner decision 2026-11-17: the colleagues are credited, the author stays first). The files are
synthetic; logins and names are invented."""
import tests  # noqa: F401 — ignore the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import unittest

from tools import contributors as c
from tools import publish_allow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTHOR = "Created by Alice Smith ([@alice](https://github.com/alice))"
CHANGES = """# What's new

An item from someone else's PR ends with thanks: "— thanks: @login (PR #N)".

## 2026-03-02
- one — thanks: @bob (PR #7)
- two, wrapped — thanks: @carol
  (PR #8)
- three — thanks: @bob, @dave-x (PR #9, #10)
- four — thanks: @bob (PR #7, partly)
- five — thanks: @carol (PR #3 of the modules)
- six — thanks: @dave-x (issue #4)
- seven — thanks: @erin
- eight — thanks: @Erin (a bug report over the bus)
- nine — thanks: another hub
- ten — thanks: @alice (PR #11)
- eleven — thanks: @frank (PR #12 and #13)
"""


class Parse(unittest.TestCase):
    def test_every_form_of_a_thanks(self):
        got = c.clauses(CHANGES)
        self.assertEqual(got, [(["bob"], {("pr", 7)}), (["carol"], {("pr", 8)}),
                               (["bob", "dave-x"], {("pr", 9), ("pr", 10)}), (["bob"], {("pr", 7)}),
                               (["carol"], {("modules-pr", 3)}), (["dave-x"], {("issue", 4)}), (["erin"], set()),
                               (["Erin"], set()), (["alice"], {("pr", 11)}), (["frank"], {("pr", 12), ("pr", 13)})])

    def test_the_tally_counts_a_pull_request_once_and_leaves_the_author_out(self):
        # most first; a tie — more pull requests first, then the login; a login keeps its first spelling
        self.assertEqual(c.tally(CHANGES, "Alice"),
                         [("bob", 3, 0), ("dave-x", 2, 1), ("carol", 2, 0), ("frank", 2, 0), ("erin", 0, 2)])
        self.assertIn(("alice", 1, 0), c.tally(CHANGES))

    def test_the_author_line(self):
        self.assertEqual(c.author_of(f"# Hub\n\n{AUTHOR}\n"), ("Alice Smith", "alice"))
        self.assertIsNone(c.author_of("Created by Alice Smith ([@alice](https://github.com/bob))\n"))
        self.assertIsNone(c.author_of("Created by Alice Smith (@alice)\n"))
        self.assertIsNone(c.author_of(f"x {AUTHOR}\n"))
        # owner 18.11: the author only in the section, not at the top — the section's own line counts
        self.assertEqual(c.author_of(f"# Hub\n\n## Contributors\n\n{AUTHOR}\n"), ("Alice Smith", "alice"))
        self.assertEqual(c.author_of(f"## Contributors\r\n\r\n{AUTHOR}\r\n"), ("Alice Smith", "alice"))   # CRLF
        # a line above the section (the older layout) wins over the section's copy
        older = "Created by Bob Jones ([@bob](https://github.com/bob))"
        self.assertEqual(c.author_of(f"# Hub\n\n{older}\n\n## Contributors\n\n{AUTHOR}\n"), ("Bob Jones", "bob"))
        # a hand-written heading of the same name earlier: the built section is the last one
        readme = f"# Hub\n\n{AUTHOR}\n\n## Contributors\n\nSee below.\n\n## Use\n\nRun.\n\n## Contributors\n\n{AUTHOR}\n"
        self.assertEqual(c.own_part(readme), readme[:readme.rindex("## Contributors")])

    def test_no_gh_is_no_id_not_a_crash(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"PATH": ""}):
            self.assertIsNone(c.gh_id("bob"))


class Coauthors(unittest.TestCase):
    def fetch(self, login):
        self.asked.append(login)
        return {"bob": 101, "carol": 102, "dave-x": 103, "erin": 104}.get(login.lower())

    def setUp(self):
        self.asked = []

    def test_the_first_commit_credits_everyone_and_the_cache_saves_the_asking(self):
        ids = {"bob": 101}
        lines, missing = c.coauthors(CHANGES, None, "alice", ids, self.fetch)
        self.assertEqual(lines, ["Co-authored-by: bob <101+bob@users.noreply.github.com>",   # in the list's order
                                 "Co-authored-by: dave-x <103+dave-x@users.noreply.github.com>",
                                 "Co-authored-by: carol <102+carol@users.noreply.github.com>",
                                 "Co-authored-by: erin <104+erin@users.noreply.github.com>"])
        self.assertEqual(missing, ["frank"])   # no id: not a co-author, said by the caller
        self.assertEqual(self.asked, ["dave-x", "carol", "frank", "erin"])
        self.assertEqual(ids, {"bob": 101, "carol": 102, "dave-x": 103, "erin": 104})

    def test_a_later_commit_credits_only_the_new_thanks(self):
        new = CHANGES.replace("## 2026-03-02", "## 2026-03-03\n- twelve — thanks: @carol (PR #14)\n"
                                               "- thirteen — thanks: @alice\n\n## 2026-03-02")
        lines, _ = c.coauthors(new, CHANGES, "alice", {}, self.fetch)
        self.assertEqual(lines, ["Co-authored-by: carol <102+carol@users.noreply.github.com>"])
        self.assertEqual(c.coauthors(CHANGES, CHANGES, "alice", {}, self.fetch)[0], [])
        # the open tree's CHANGES.md has no numbers: told by how many thanks name a login
        cut = new.replace(" (PR #14)", "")
        self.assertEqual(c.coauthors(cut, CHANGES.replace(" (PR #7)", ""), "alice", {}, self.fetch)[0],
                         ["Co-authored-by: carol <102+carol@users.noreply.github.com>"])

    def test_a_history_that_credits_four_leaves_the_missing_twenty_one_and_a_second_run_none(self):
        # the snapshots before the credits (or with an unknown id) carry no trailers: the next commit names everyone
        # the thanks name and no commit credits yet, however old's counts stand; a credited login is never repeated
        logins = [f"user{n:02d}" for n in range(25)]
        changes = "## 2026-03-02\n" + "".join(f"- item — thanks: @{x} (PR #{n + 1})\n" for n, x in enumerate(logins))
        fetch = lambda login: 500 + int(login[4:])   # noqa: E731
        history = "Sync 1\n\nCo-authored-by: user00 <500+user00@users.noreply.github.com>\n" \
                  "Co-authored-by: User01 <501+User01@users.noreply.github.com>\n\nSync 0\n" \
                  "Co-authored-by: user02 <502+user02@users.noreply.github.com>\n" \
                  "Co-authored-by: Somebody Named user03 <user03@example.test>\n" \
                  "Co-authored-by: user03 <ignored@example.test>\n"
        lines, missing = c.coauthors(changes, changes, "alice", {}, fetch, history=history)   # the thanks are not new
        self.assertEqual(missing, [])
        self.assertEqual([x.split()[1] for x in lines], logins[4:] + [])   # user03 by the name, 00-02 by the address
        self.assertEqual(len(lines), 21)
        again, _ = c.coauthors(changes, changes, "alice", {}, fetch, history=history + "\n".join(lines))
        self.assertEqual(again, [])
        # no history known: the old rule, nobody new — nobody named
        self.assertEqual(c.coauthors(changes, changes, "alice", {}, fetch)[0], [])
        # a thanks anew of a credited login is not a second trailer; the author is no co-author
        more = changes + "- more — thanks: @user00 (PR #99)\n- mine — thanks: @alice (PR #98)\n"
        self.assertEqual(c.coauthors(more, changes, "alice", {}, fetch, history=history + "\n".join(lines))[0], [])

    def test_the_lists_logins_the_thanks_do_not_name_are_added_with_a_history(self):
        got, _ = c.coauthors(CHANGES, CHANGES, "alice", {"zed": 9, "bob": 101}, self.fetch, history="Sync\n",
                             extra=["bob", "zed", "alice"])
        self.assertEqual([x.split()[1] for x in got], ["bob", "dave-x", "carol", "erin", "zed"])   # frank: no id


class Write(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="contributors-")
        self.addCleanup(tmp.cleanup)
        self.tree = tmp.name
        self.put("CHANGES.md", CHANGES)
        self.put("README.md", f"# Hub\n\n{AUTHOR}\n\n## Use\n\nRun it.\n")

    def put(self, name, text):
        with open(os.path.join(self.tree, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def get(self, name):
        with open(os.path.join(self.tree, name), encoding="utf-8") as f:
            return f.read()

    def test_the_section_and_the_file_are_built_and_built_again_the_same(self):
        self.assertEqual(c.write(self.tree), "changed")
        readme = self.get("README.md")
        self.assertTrue(readme.startswith(f"# Hub\n\n{AUTHOR}\n\n## Use\n\nRun it.\n\n## Contributors\n\n{AUTHOR}\n"))
        self.assertTrue(readme.endswith("- [@erin](https://github.com/erin): 2 reports and ideas\n"), readme)
        listing = self.get("CONTRIBUTORS.md")
        self.assertTrue(listing.startswith(f"# Contributors\n\n{AUTHOR}\n"))
        self.assertIn("- [@bob](https://github.com/bob): 3 pull requests\n"
                      "- [@dave-x](https://github.com/dave-x): 2 pull requests, 1 report or idea\n"
                      "- [@carol](https://github.com/carol): 2 pull requests\n"
                      "- [@frank](https://github.com/frank): 2 pull requests\n"
                      "- [@erin](https://github.com/erin): 2 reports and ideas\n", listing)
        self.assertNotIn("[@alice]", listing.split(AUTHOR, 1)[1])   # the author is not one of the list
        self.assertEqual(c.write(self.tree), "same")
        self.put("CHANGES.md", CHANGES + "- fourteen — thanks: @gina (PR #20)\n")
        self.assertEqual(c.write(self.tree), "changed")
        self.assertEqual(self.get("README.md").count("## Contributors"), 1)   # the section is replaced, not added
        self.assertIn("[@gina]", self.get("README.md"))

    def test_the_author_only_in_the_section_stays_there_and_not_at_the_top(self):
        # owner 18.11: the author line moved from the top of the README to its Contributors section; a rebuild keeps it
        # at the section's head and adds none above
        self.put("README.md", f"# Hub\n\n## Use\n\nRun it.\n\n## Contributors\n\n{AUTHOR}\n\n- [@old](https://github.com/old): 1 pull request\n")
        self.assertEqual(c.write(self.tree), "changed")
        readme = self.get("README.md")
        self.assertTrue(readme.startswith(f"# Hub\n\n## Use\n\nRun it.\n\n## Contributors\n\n{AUTHOR}\n\nWith "), readme)
        self.assertEqual(readme.count(AUTHOR), 1)
        self.assertNotIn("[@old]", readme)   # the list is rebuilt from CHANGES.md
        self.assertTrue(self.get("CONTRIBUTORS.md").startswith(f"# Contributors\n\n{AUTHOR}\n"))
        self.assertEqual(c.write(self.tree), "same")

    def test_no_author_line_writes_nothing_and_the_command_refuses(self):
        self.put("README.md", "# Hub\n")
        self.assertEqual(c.write(self.tree), "no author")
        self.assertFalse(os.path.exists(os.path.join(self.tree, "CONTRIBUTORS.md")))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "contributors.py"), "write", self.tree],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.put("README.md", f"# Hub\n\n## Contributors\n\nSee below.\n")   # a section without the line
        self.assertEqual(c.write(self.tree), "no author")
        os.unlink(os.path.join(self.tree, "README.md"))
        self.assertIsNone(c.write(self.tree))   # a tree with no README: nothing to do

    def test_what_it_writes_passes_the_publish_check_and_the_names_elsewhere_do_not(self):
        c.write(self.tree)
        self.put("notes.md", "ask Alice or bob\n")
        deny = os.path.join(self.tree, "deny")
        with open(deny, "w", encoding="utf-8") as f:
            f.write("alice|smith|bob|carol|dave|erin|frank\n")
        files = [x for x in publish_allow.tree(self.tree) if x[0] not in ("CHANGES.md", "deny")]   # only what it wrote
        out = list(publish_allow.denied(files, publish_allow.patterns(deny, True), []))
        self.assertEqual(out, ["notes.md:1:ask Alice or bob\n"])
        from core import appcode
        self.assertEqual(appcode.open_guard(self.tree, ["alice", "bob"], spare=publish_allow.credit_file),
                         ["CHANGES.md: carries a forbidden string", "deny: carries a forbidden string",
                          "notes.md: carries a forbidden string"])
        self.put("README.md", AUTHOR)   # a file of credits alone is spared whole (nothing left of it is still something)
        self.assertNotIn("README.md: carries a forbidden string", appcode.open_guard(self.tree, ["alice"],
                                                                                   spare=publish_allow.credit_file))

    def test_the_cli_prints_the_trailers_and_keeps_the_ids_beside_the_publishing_state(self):
        state = os.path.join(self.tree, "state")
        os.makedirs(state)
        with open(os.path.join(state, "github-ids.json"), "w", encoding="utf-8") as f:
            json.dump({"bob": 101, "carol": 102, "dave-x": 103, "erin": 104, "frank": 105}, f)   # no gh call
        def cli(*args):
            return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "contributors.py"), "coauthors",
                                   self.tree, *args], capture_output=True, text=True,
                                  env={**os.environ, "HUB_STATE_DIR": state, "PATH": ""})   # no gh: the cache only
        r = cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "Co-authored-by: bob <101+bob@users.noreply.github.com>")
        self.assertEqual(len(r.stdout.splitlines()), 5)
        self.assertNotIn("alice", r.stdout)
        self.put("CHANGES.md", CHANGES + "- fourteen — thanks: @gina (PR #20)\n")   # gina has no id: gh is not there
        r = cli()
        self.assertEqual(r.returncode, 0, r.stderr)   # said, not refused: a typo must not stop every release after it
        self.assertIn("WARNING: contributors: no GitHub id for @gina", r.stderr)
        self.assertNotIn("gina", r.stdout)
        self.assertEqual(len(r.stdout.splitlines()), 5)

    def test_the_cli_reads_the_history_of_a_clone(self):
        state = os.path.join(self.tree, "state")
        os.makedirs(state)
        with open(os.path.join(state, "github-ids.json"), "w", encoding="utf-8") as f:
            json.dump({"bob": 101, "carol": 102, "dave-x": 103, "erin": 104, "frank": 105}, f)
        clone = os.path.join(self.tree, "clone")
        git = lambda *a: subprocess.run(["git", "-C", clone, "-c", "user.name=t", "-c", "user.email=t@example.test", *a],   # noqa: E731
                                        check=True, capture_output=True, text=True)
        os.makedirs(clone)
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "Sync\n\nCo-authored-by: bob <101+bob@users.noreply.github.com>")
        old = os.path.join(self.tree, "old.md")
        with open(old, "w", encoding="utf-8") as f:
            f.write(CHANGES)   # nothing is new by the counts

        def cli(*args):
            return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "contributors.py"), "coauthors",
                                   self.tree, "--old", old, *args], capture_output=True, text=True,
                                  env={**os.environ, "HUB_STATE_DIR": state, "PATH": os.environ["PATH"]})
        self.assertEqual(cli().stdout, "")
        r = cli("--history", clone)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x.split()[1] for x in r.stdout.splitlines()], ["dave-x", "carol", "frank", "erin"])
        self.assertEqual(cli("--history", os.path.join(self.tree, "nowhere")).returncode, 3)


if __name__ == "__main__":
    unittest.main()
