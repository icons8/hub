"""Refusals of `hub` by its parameters say what is wrong and how it is right, and the forms sessions really type are taken
(13.11, the transcripts and the journal of a week: about 173 refusals by parameters in about 100 sessions). One class of
that week each: critic evidence separated by spaces (39) and `~` after a comma (16), a guessed command or subcommand,
a flag of another command (13), the built-in backlog (14: `--help` after a task id, `-c` for a document), `schedule add`
(argparse's words), `tools request` without a reason (4), a mini-hub's word to its executor without `--about` (19),
`--for` (11: one phrase for every reason), an executor's name taken or too long (9)."""
import tests  # noqa: F401 — ignore the machine's settings
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core import journal, reducer, spool, tools
from modules.critic import critic
from tests import test_minihub   # its helpers, not its tests (a module runs only its own)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")


def run_hub(*args, me="maps", state=None, stdin=None):
    """bin/hub on a state of its own with the sessions maps and geo: (result, the facts it put)."""
    if state is None:
        state = tempfile.mkdtemp(prefix="clierr-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        for n in ("maps", "geo"):
            j.append("session.requested", n, {"cwd": "/w", "session_id": f"s-{n}", "channel": "cli", "owner": True})
    env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_LANG": "en", "PYTHONUTF8": "1"}
    env.pop("HUB_SESSION", None)
    if me:
        env["HUB_SESSION"] = me
    r = subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True, encoding="utf-8",
                       input=stdin, timeout=60, **({} if stdin is not None else {"stdin": subprocess.DEVNULL}))
    return r, [ev for _, ev in spool.take(os.path.join(state, "spool"))]


def files(*names):
    d = tempfile.mkdtemp(prefix="clierr-f-")
    out = []
    for n in names:
        out.append(os.path.join(d, n))
        with open(out[-1], "w") as f:
            f.write("x")
    return out


class Critic(unittest.TestCase):
    """39 runs a week gave `--evidence a b c` (the rule said `[--evidence <paths>]`), 16 — `~` after a comma (the shell
    expands it only at the start of a word): both were refused or pointed at no file."""

    def main(self, argv):
        seen = []
        err = io.StringIO()
        with mock.patch.object(critic, "review", lambda cfg, d, e, t: seen.append((d, e, t)) or ("x", "ok")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = critic.main(argv, {"critic": {"enabled": True}})
        return code, seen, err.getvalue()

    def test_evidence_with_spaces_commas_or_the_flag_again_is_one_list(self):
        d, a, b, c = files("d.md", "a.py", "b.py", "c.py")
        for argv in (["--evidence", a, b, c], ["--evidence", f"{a},{b}", c], ["--evidence", a, "--evidence", f"{b},{c}"]):
            code, seen, err = self.main([d, "--task", "t", *argv])
            self.assertEqual((code, seen), (0, [(d, [a, b, c], "t")]), (argv, err))
        code, seen, _ = self.main([d, "--evidence", a, b, "--task", "t"])   # up to the next flag
        self.assertEqual(seen, [(d, [a, b], "t")])

    def test_a_tilde_in_any_path_is_the_home_folder(self):
        home = tempfile.mkdtemp(prefix="clierr-home-")
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):   # Windows reads USERPROFILE
            code, seen, _ = self.main(["~/d.md", "--evidence", "~/a.py,~/b.py"])
        n = os.path.normpath   # Windows: "<home>/d.md" is the same path as "<home>\\d.md"
        self.assertEqual([(n(d), [n(x) for x in e], t) for d, e, t in seen],
                         [(os.path.join(home, "d.md"), [os.path.join(home, "a.py"), os.path.join(home, "b.py")], None)])

    def test_a_second_draft_is_named_with_the_form(self):
        code, seen, err = self.main(["a.md", "b.md", "--task", "t"])
        self.assertEqual((code, seen), (2, []))
        self.assertIn("«b.md»", err)
        self.assertIn("hub critic <draft>", err)
        self.assertIn("e.g. hub critic /home/x/", err)

    def test_a_flag_after_evidence_is_not_a_path(self):
        code, seen, err = self.main(["d.md", "--evidence", "--task", "t"])
        self.assertEqual((code, seen), (2, []))
        self.assertIn("--evidence needs a value", err)

    def test_a_help_that_cannot_be_read_leaves_the_rule_with_its_form(self):
        from core import clihelp
        with mock.patch.object(clihelp, "load", side_effect=clihelp.Bad("3: broken")):
            note = critic.note()
        self.assertIn("`hub critic <draft>", note)
        self.assertNotIn("{", note)

    def test_the_rule_in_the_prompt_is_the_helps_form_and_example(self):
        from core import clihelp
        e = clihelp.load().entry("critic")
        self.assertIn(f"`{e.forms[0]}`", critic.NOTE)
        self.assertIn(f"`{e.examples[0]}`", critic.NOTE)
        self.assertRegex(e.examples[0], r"--evidence \S+,\S+")


class Guessed(unittest.TestCase):
    """A guessed command or subcommand got the whole help, with no word of what was wrong."""

    def test_an_unknown_subcommand_is_named_with_the_closest(self):
        r, evs = run_hub("tools", "requests")
        self.assertEqual(evs, [])
        self.assertIn("no subcommand «requests»", r.stderr)
        self.assertIn("closest: request", r.stderr)
        r, _ = run_hub("wait", "list")
        self.assertIn("no subcommand «list»; it has: done", r.stderr)

    def test_a_command_sessions_invent_names_what_answers(self):
        for word, want in (("list", "hub status"), ("whoami", "$HUB_SESSION"), ("inbox", "hub show <your name>")):
            r, _ = run_hub(word)
            self.assertIn(f"no command «{word}»", r.stderr)
            self.assertIn(want, r.stderr, word)
            self.assertNotIn("Sessions:", r.stderr, word)   # not the whole index

    def test_version_as_a_flag(self):
        self.assertEqual(run_hub("--version")[0].stdout, run_hub("version")[0].stdout)

    def test_words_that_do_not_fit_say_so_with_the_form(self):
        r, evs = run_hub("session", "close")
        self.assertEqual(evs, [])
        self.assertIn("hub session close: the words do not fit its form", r.stderr)
        self.assertIn("e.g. hub session close maps", r.stderr)


class ForeignFlag(unittest.TestCase):
    """A flag of another command (send-file --caption, shot -o, session wake --work/--file, schedule --text, wait --every,
    task --branch-from): the refusal names the common mistake or the closest flag, the form and an example."""

    def test_the_common_mistake_and_the_form(self):
        for args, want in ((("send-file", "a.png", "--caption", "x"), "the caption goes as the last word"),
                           (("shot", "--compare", "a", "b", "-o", "c"), "only --out"),
                           (("schedule", "add", "--at", "10:00", "--text", "x"), "--prompt"),
                           (("wait", "the answer", "--every", "1h"), "there is no --every"),
                           (("task", "note", "--task", "d1", "x"), "hub task note d12"),
                           (("task", "run", "x", "do", "--work", "normal", "--branch-from", "main"), "--from-main"),
                           (("session", "wake", "geo", "--work", "risky", "x"), "there is no --work here"),
                           (("show", "maps", "--messages"), "-n N, --full"),
                           (("critic", "d.md", "--engine", "claude"), "the model is the hub's setting")):
            r, evs = run_hub(*args)
            self.assertEqual(evs, [], args)
            self.assertIn(want, r.stderr, args)
            self.assertIn(f"  hub {args[0]}", r.stderr, args)   # the form

    def test_a_close_flag(self):
        r, _ = run_hub("task", "run", "x", "do", "--work", "normal", "--repp", "/tmp")
        self.assertIn("closest: --repo", r.stderr)

    def test_wake_takes_the_text_from_a_file_or_stdin(self):
        (path,) = files("t.txt")
        with open(path, "w") as f:
            f.write("fix the `make tiles` step")
        for args, stdin in ((("--file", path), None), (("-",), "fix the `make tiles` step")):
            r, evs = run_hub("session", "wake", "geo", *args, me=None, stdin=stdin)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual([(e["type"], e["data"]["text"], e["data"]["wake"]) for e in evs],
                             [("message.received", "fix the `make tiles` step", True)])


class Backlog(unittest.TestCase):
    """The direction's built-in backlog: `task edit TASK-7 --help` was an unknown option whose hint said --help again;
    `doc create -c` had no text; --note/--notes-append/--append/--grep got the list of flags only."""

    def setUp(self):
        from modules.directions import minibacklog
        self.b = minibacklog
        self.cwd = tempfile.mkdtemp(prefix="clierr-bl-")
        os.environ["HUB_STATE_DIR"], self.was = tempfile.mkdtemp(prefix="clierr-st-"), os.environ.get("HUB_STATE_DIR")
        self.run_(["init", "x"])
        self.run_(["task", "create", "first"])

    def tearDown(self):
        if self.was is None:
            os.environ.pop("HUB_STATE_DIR", None)
        else:
            os.environ["HUB_STATE_DIR"] = self.was

    def run_(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.b.main(argv, self.cwd)
        return code, out.getvalue() + err.getvalue()

    def test_help_after_the_words(self):
        for argv in (["task", "edit", "TASK-1", "--help"], ["doc", "update", "doc-1", "-h"], ["task", "list", "--help"]):
            code, out = self.run_(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("task edit <id>", out, argv)

    def test_help_as_an_options_value_is_the_value(self):
        code, out = self.run_(["task", "edit", "TASK-1", "--append-notes", "-h"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("task edit <id>", out)
        self.assertIn("-h", self.run_(["task", "view", "TASK-1"])[1])

    def test_a_document_with_its_text(self):
        for flag in ("-c", "--content"):
            code, out = self.run_(["doc", "create", f"Notes {flag}", flag, "- the first line"])
            self.assertEqual(code, 0, out)
            doc = out.split()[2]
            self.assertIn("- the first line", self.run_(["doc", "view", doc])[1])

    def test_a_documents_text_from_a_file_or_stdin(self):
        """A long document in --content went over the command line's limit (Windows: about 32K, "Argument list too long"):
        `--content -` reads the text from stdin, `--content-file <path>` from a file; empty is refused as an empty --content."""
        long = "\n".join(f"- line {i} of a long document" for i in range(2000))
        path = os.path.join(self.cwd, "text.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(long + "\n")

        def stdin(text):
            return mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(text.encode()), encoding="utf-8"))

        code, out = self.run_(["doc", "create", "From a file", "--content-file", path])
        self.assertEqual(code, 0, out)
        doc = out.split()[2]
        self.assertIn(long, self.run_(["doc", "view", doc])[1])
        with stdin("- from stdin\n"):
            code, out = self.run_(["doc", "update", doc, "--content", "-"])
        self.assertEqual(code, 0, out)
        text = self.run_(["doc", "view", doc])[1]
        self.assertIn("- from stdin", text)
        self.assertNotIn("line 1999", text)
        with stdin("- created from stdin\n"):
            code, out = self.run_(["doc", "create", "From stdin", "-c", "-"])
        self.assertEqual(code, 0, out)
        other = out.split()[2]
        self.assertIn("- created from stdin", self.run_(["doc", "view", other])[1])
        code, out = self.run_(["doc", "update", other, "--content-file", path])
        self.assertEqual(code, 0, out)
        self.assertIn(long, self.run_(["doc", "view", other])[1])
        empty = os.path.join(self.cwd, "empty.md")
        open(empty, "w").close()
        for argv, given in ((["doc", "update", doc, "--content-file", empty], None),
                            (["doc", "update", doc, "--content", "-"], "  \n")):
            with stdin(given or ""):
                code, out = self.run_(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("is empty", out, argv)
        self.assertIn("- from stdin", self.run_(["doc", "view", doc])[1])   # the refused updates wrote nothing
        code, out = self.run_(["doc", "update", doc, "--content-file", os.path.join(self.cwd, "missing.md")])
        self.assertEqual(code, 2, out)
        self.assertIn("missing.md", out)

    def test_what_was_meant_by_an_unknown_option(self):
        for argv, want in ((["task", "edit", "1", "--note", "x"], "--append-notes adds"),
                           (["task", "edit", "1", "--notes-append", "x"], "--append-notes adds"),
                           (["doc", "update", "doc-1", "--append", "x"], "--content replaces the whole text"),
                           (["task", "list", "--grep", "x"], "| grep"),
                           (["task", "edit", "1", "--priorty", "high"], "closest: --priority")):
            code, out = self.run_(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(want, out, argv)


class Schedule(unittest.TestCase):
    def test_argparse_words_with_the_common_mistake(self):
        r, evs = run_hub("schedule", "add", "--at", "2026-11-07 10:00", "check the deploy")
        self.assertEqual(evs, [])
        self.assertIn("the text goes after --prompt", r.stderr)
        self.assertIn("e.g. hub schedule add", r.stderr)
        for when in (("--every", "weekdays", "--at", "10:30"), ("--at", "10:30", "--every", "weekdays")):   # either order
            r, _ = run_hub("schedule", "add", "--id", "x", *when, "--command", "true")
            self.assertIn('--cron "30 10 * * 1-5"', r.stderr, when)
            self.assertEqual(r.returncode, 2)   # argparse's code for a usage error


class ToolsRequest(unittest.TestCase):
    def test_without_a_reason_says_that_from_a_session(self):
        r, evs = run_hub("tools", "request", "browser:mac")
        self.assertEqual(evs, [])
        self.assertNotIn("is a session command", r.stderr)
        self.assertIn("why you need it", r.stderr)
        r, _ = run_hub("tools", "request", "browser:mac", me=None)
        self.assertIn("is a session command", r.stderr)


class MiniHubWord(test_minihub.Delegating):
    """A mini-hub's word to its executor asleep after the hand-in: `session wake X "…"` without --about (10 a week) and
    `send X "…"` (9) were refused, though the executor had one task from it — the one the refusal then named."""

    hub_cli = test_minihub.Review2Cli.hub_cli
    asleep = test_minihub.Review2Cli.asleep

    def test_wake_without_about_takes_the_one_task(self):
        sub = self.asleep()
        r, got = self.hub_cli("session", "wake", "notes-ci", "finish the tests", me="notes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(e["type"], e["data"].get("about"), e["data"].get("wake")) for e in got],
                         [("message.received", sub, True)])
        self.assertIn(sub, r.stdout)
        self.assertIsNone(self.ev(got[0]["type"], got[0]["entity"], **got[0]["data"]))   # the core takes it

    def test_a_message_to_the_own_sleeper_is_its_tasks_rework(self):
        sub = self.asleep()
        r, got = self.hub_cli("send", "notes-ci", "finish the tests", me="notes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(e["data"].get("about"), e["data"].get("wake")) for e in got], [(sub, True)])
        self.assertIsNone(self.ev(got[0]["type"], got[0]["entity"], **got[0]["data"]))

    def test_a_raiser_that_is_not_a_mini_hub_is_refused_as_before(self):
        sub = self.asleep()
        self.ev("session.solo_set", "notes", on=True, **{"channel": "cli", "owner": True})   # "solo": no mini-hub
        r, got = self.hub_cli("send", "notes-ci", "finish the tests", me="notes")
        self.assertEqual([e["data"].get("about") for e in got], [None])
        self.assertIn(sub, self.ev(got[0]["type"], got[0]["entity"], **got[0]["data"]) or "")   # the core's refusal

    def test_others_and_fyi_as_before(self):
        self.asleep()
        for args, me in ((("send", "notes-ci", "--fyi", "CI green"), "notes"), (("send", "notes-ci", "hi"), "api-x"),
                         (("session", "wake", "notes-ci", "go on"), "api-x"), (("session", "wake", "notes-ci", "go"), None)):
            r, got = self.hub_cli(*args, me=me)
            self.assertEqual([e["data"].get("about") for e in got], [None], (args, me))


class ForReason(unittest.TestCase):
    """`--for`: which condition failed, before the rule (one phrase for three reasons, and the same --for again)."""

    def test_the_reason_first(self):
        st = reducer.empty()
        st["sessions"]["hub"] = {"name": "hub", "kind": "hub", "state": "idle"}
        st["sessions"]["maps"] = {"name": "maps", "state": "idle"}
        st["messages"]["m5"] = {"id": "m5", "from_owner": True, "created": 0, "session": "hub", "text": "yes"}
        st["messages"]["m6"] = {"id": "m6", "from_owner": False, "created": 0, "session": "hub", "text": "yes"}
        st["messages"]["m7"] = {"id": "m7", "from_owner": True, "created": 0, "session": "geo", "text": "yes"}
        hub = {"sender": "hub", "channel": "session"}
        for ref, ts, want in (("m9", 10, "--for m9: no such message"), ("m6", 10, "--for m6: these are not the owner's"),
                              ("m5", 3 * 86400, "--for m5: 72 h old"), ("m7", 10, "the owner wrote it to geo"),
                              ("q4", 10, "--for q4: no answer of the owner to this card")):
            with self.assertRaises(tools.Rejected) as e:
                tools.owner_word(st, {**hub, "for": ref}, ts, cards=True)
            self.assertIn(want, str(e.exception), ref)
            self.assertIn("needs the owner's message to the hub", str(e.exception), ref)
        self.assertEqual(tools.owner_word(st, {**hub, "for": "m5"}, 10)["message"], "m5")
        with self.assertRaises(tools.Rejected) as e:   # no --for at all: the rule alone, as before
            tools.owner_word(st, hub, 10)
        self.assertTrue(str(e.exception).startswith("--for: needs"), str(e.exception))


    def test_taking_off_counts_his_word_to_any_session_for_the_hub_only(self):
        st = reducer.empty()
        st["sessions"]["hub"] = {"name": "hub", "kind": "hub", "state": "idle"}
        st["sessions"]["maps"] = {"name": "maps", "state": "idle"}
        st["messages"]["m7"] = {"id": "m7", "from_owner": True, "created": 0, "session": "maps", "text": "remove it"}
        st["messages"]["m8"] = {"id": "m8", "from_owner": True, "created": 0, "session": "maps", "text": "x",
                                "card_declined": True, "answers_question": "q4"}
        st["messages"]["m9"] = {"id": "m9", "from_owner": False, "created": 0, "session": "maps", "text": "remove it"}
        hub = {"sender": "hub", "channel": "session"}
        self.assertEqual(tools.owner_word(st, {**hub, "for": "m7"}, 10, cards=True, lift=True)["message"], "m7")
        with self.assertRaises(tools.Rejected) as e:   # without lift (install, grant, run): as before
            tools.owner_word(st, {**hub, "for": "m7"}, 10, cards=True)
        self.assertIn("the owner wrote it to maps", str(e.exception))
        for sender, ref, ts, want in (("maps", "m7", 10, "needs the owner's message to the hub"), ("hub", "m7", 3 * 86400, "72 h old"),
                                      ("hub", "m9", 10, "not the owner's"), ("hub", "m8", 10, "\"Deny\"")):
            with self.subTest(sender=sender, ref=ref):
                with self.assertRaises(tools.Rejected) as e:
                    tools.owner_word(st, {"sender": sender, "channel": "session", "for": ref}, ts, cards=True, lift=True)
                self.assertIn(want, str(e.exception))


class ExecutorName(unittest.TestCase):
    def test_a_taken_name_comes_with_a_free_one(self):
        from core import executors
        st = reducer.empty()
        for n in ("maps-fix", "maps-fix-2"):
            st["sessions"][n] = {"name": n, "state": "idle"}
        self.assertIn("a free one: maps-fix-3", str(executors.check(st, "maps-fix", None)))
        self.assertIn("a free one: maps-fix-3", str(executors.check(st, "maps-fix-2", None)))   # not maps-fix-2-2
        st["sessions"]["release-2026"] = {"name": "release-2026", "state": "idle"}
        self.assertIn("a free one: release-2026-2", str(executors.check(st, "release-2026", None)))   # a year stays
        long = "y" * 45
        st["sessions"]["y" * 40] = {"name": "y" * 40, "state": "idle"}
        st2, why = reducer.reduce(st, {"seq": 1, "ts": 1, "type": "task.run_requested", "entity": long,
                                       "data": {"task": "x", "channel": "cli", "owner": True}})[0::2]
        self.assertIn("e.g. " + "y" * 38 + "-2", str(why))   # the cut is taken: a free one
        self.assertEqual(len(executors.free_name(st, "x" * 40)), 40)
        self.assertEqual(executors.free_name(st, "x" * 40, most=reducer.NAME_MAX), "x" * 38 + "-2")


if __name__ == "__main__":
    unittest.main()
