"""`hub` at the edges: a core refusal ends the command with a nonzero code, bad arguments give a line with the rule
rather than a traceback or an empty fact in the inbox; the help names what exists and does not name what does not."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from core import i18n, journal, langdata, restart, spool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "bin", "hub")
OWN = {"channel": "cli", "owner": True}


class FakeCore(threading.Thread):
    """Records what the command put in the inbox, like hubd: a fact, then a refusal if `reject` says so."""

    def __init__(self, state, reject=lambda ev: None):
        super().__init__(daemon=True)
        self.state, self.reject, self.stop = state, reject, threading.Event()

    def run(self):
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        while not self.stop.is_set():
            for path, ev in spool.take(os.path.join(self.state, "spool")):
                seq = j.append(ev["type"], ev.get("entity"), ev.get("data"), src="cli")["seq"]
                why = self.reject(ev)
                if why:
                    j.append("core.rejected", ev.get("entity"), {"of": seq, "type": ev["type"], "reason": why})
                os.unlink(path)
            time.sleep(0.02)


class Base(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="cli-edges-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for n in ("a", "b"):
            self.j.append("session.requested", n, {"cwd": self.state, "session_id": f"s-{n}", **OWN})
            self.j.append("session.started", n, {"session_id": f"s-{n}"})
        self.cfg = os.path.join(tempfile.mkdtemp(), "hub.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({}, f)
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CONFIG": self.cfg, "HUB_CLI_WAIT": "0"}
        for k in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE", "HUB_WEB_PORT"):
            self.env.pop(k, None)

    def hub(self, *args, me=None, stdin=None, **env):
        e = {**self.env, **env}
        if me:
            e["HUB_SESSION"] = me
        return subprocess.run([sys.executable, HUB, *args], env=e, capture_output=True, text=True, encoding="utf-8",
                              timeout=30, input=stdin, **({} if stdin is not None else {"stdin": subprocess.DEVNULL}))

    def spooled(self):
        """What the command put in the inbox since the last look."""
        out = []
        for path, ev in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            out.append(ev)
        return out

    def core(self, **kw):
        c = FakeCore(self.state, **kw)
        c.start()
        self.addCleanup(c.stop.set)
        return c

    def refused(self, r, spooled=()):
        """A call error: nonzero code, a line for the human, no traceback, nothing (new) in the inbox."""
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual([e["type"] for e in self.spooled()], list(spooled))


class RejectedExitCode(Base):
    """The core refused — the command says so and ends with code 1: `&&` and scripts can rely on it."""

    def test_rejected_commands_fail(self):
        self.core(reject=lambda ev: "not yours")
        for args in (("task", "done", "d1"), ("decided", "x · y"), ("session", "close", "b"), ("away",),
                     ("schedule", "add", "--session", "b", "--every", "1h", "--prompt", "p"), ("back",),
                     ("waiting", "vendor reply"), ("wait", "vendor reply")):
            r = self.hub(*args, me="a", HUB_CLI_WAIT="5")
            self.assertIn("not yours", r.stdout, args)
            self.assertEqual(r.returncode, 1, (args, r.stdout, r.stderr))

    def test_accepted_command_succeeds(self):
        self.core()
        r = self.hub("decided", "x · y", me="a", HUB_CLI_WAIT="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Arguments(Base):
    """A flag without a value, an empty message, a word where a number belongs."""

    def test_decided_does_not_record_a_command_word_as_a_decision(self):
        """`hub decided list` once recorded the decision "list": a lone reserved word or flag is refused with a hint."""
        for args in (("ls",), ("show",), ("undo",), ("undo", "m5"), ("list", "5"), ("show", "q41"), ("help",),
                     ("LIST",), ("--all",), ("-x", "y")):
            r = self.hub("decided", *args, me="a")
            self.refused(r)
            self.assertIn(f"{args[0]}", r.stdout + r.stderr, args)   # the hint names the word, in any language
            if not args[0].startswith("-"):   # a flag is refused earlier, by the command's help (core/clihelp.py)
                self.assertIn('hub decided "', r.stdout + r.stderr, args)

    def test_reply_is_not_a_command_and_the_refusal_says_what_answers(self):
        """Sessions invent `hub reply`: the refusal is one line naming `hub send`, not the whole list of commands."""
        r = self.hub("reply", "hello", me="a")
        self.refused(r)
        out = r.stdout + r.stderr
        self.assertIn("hub send <who>", out)
        self.assertLess(len(out.splitlines()), 3, out)   # no list of commands after it
        r = self.hub("nosuchcommand", me="a")   # any other unknown command keeps the general refusal and the list
        self.refused(r)
        self.assertGreater(len((r.stdout + r.stderr).splitlines()), 10)
        self.assertNotIn("hub send <who>", r.stdout + r.stderr)

    def test_decided_undo_sends_the_withdrawal_and_names_the_restart_an_older_core_needs(self):
        self.core(reject=lambda ev: "unknown event type" if ev["type"] == "owner.decided_revoked" else None)
        r = self.hub("decided", "undo", "#42", me="a", HUB_CLI_WAIT="5")
        self.refused(r)
        self.assertIn("hub restart", r.stdout + r.stderr)   # the command name, in any language
        (ev,) = [e for e in journal.read(os.path.join(self.state, "journal.jsonl")) if e["type"] == "owner.decided_revoked"]
        self.assertEqual(ev["data"]["seq"], 42)
        self.assertFalse([e for e in journal.read(os.path.join(self.state, "journal.jsonl")) if e["type"] == "owner.decided"])

    def test_decided_list_records_nothing(self):
        r = self.hub("decided", "list", me="a")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.spooled(), [])

    def test_decided_list_with_a_card_is_not_the_local_read(self):
        # review high: the liaison may run `hub decided --card qN "…"` — with "list" it read the owner's decisions
        for args in (("list",), ("undo", "5"), ("undo", "#5")):   # and no withdrawal with a card
            r = self.hub("decided", "--card", "q5", *args, me="liaison")
            self.refused(r)
            self.assertIn('hub decided "', r.stdout + r.stderr, args)   # the reserved word's hint
            self.assertEqual(self.spooled(), [])

    def test_decided_still_records_a_phrase_that_starts_with_such_a_word(self):
        self.core()
        for text in ("list the open tasks only in the summary · saves a click · re-enable the full list",
                     "undo is not offered to sessions · one way to withdraw · say so"):
            r = self.hub("decided", text, me="a", HUB_CLI_WAIT="5")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.hub("decided", "list", "of", "tasks", "stays", "short", me="a", HUB_CLI_WAIT="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for text in ("Show thumbnails", "List view", "Undo removed", "-20% budget"):   # short, but real decisions
            r = self.hub("decided", text, me="a", HUB_CLI_WAIT="5")
            self.assertEqual(r.returncode, 0, text + ": " + r.stdout + r.stderr)

    def test_send(self):
        self.refused(self.hub("send", "--now", "--now"))
        self.refused(self.hub("send", "a", "--now"))
        self.refused(self.hub("send", "a", ""))
        self.refused(self.hub("send", "a", "  "))
        self.refused(self.hub("send", "a", "--about", "d1", " "))
        self.refused(self.hub("post", "p", " "))

    def test_send_of_only_dashes_or_punctuation_is_refused_like_an_empty_one(self):
        # a dash sent by a session command is not an answer but a loop of silent answers: no letter, digit or symbol —
        # same as empty text
        for text in ("—", " — ", "--", "...", "?!", "«»", "\u200b", "— \u2014"):
            r = self.hub("send", "b", text, me="a")
            self.refused(r)
            self.assertIn("nothing to send", r.stderr, text)   # a session's command: in English
            self.assertIn("end your turn", r.stderr, text)
        self.refused(self.hub("send", "b", "--about", "d1", "—", me="a"))
        self.refused(self.hub("post", "p", "…", me="a"))
        self.refused(self.hub("send", "b", "—", HUB_MODULE="m"))   # a module and a plugin are not the owner either
        self.refused(self.hub("send", "b", "—", HUB_EXTENSION="p"))
        # short answers as before: a word, a number, an emoji, a "+" / "✓" sign
        for text in ("ok", "yes", "1", "👍", "+", "✓", "— yes", "ok."):
            r = self.hub("send", "b", text, me="a")
            self.assertEqual(r.returncode, 0, (text, r.stdout, r.stderr))
            self.assertEqual([(e["type"], e["data"]["text"]) for e in self.spooled()], [("message.received", text)])
        # the owner's words go as is: from the terminal and passed on (--for); their "?" is a question
        for args, me in ((("send", "a", "?"), None), (("send", "b", "--for", "m1", "?"), "a")):
            r = self.hub(*args, me=me)
            self.assertEqual(r.returncode, 0, (args, r.stdout, r.stderr))
            self.assertEqual([e["data"]["text"] for e in self.spooled()], ["?"])

    @unittest.skipIf(os.name == "nt", "a Windows command line is text, not bytes")
    def test_a_byte_the_shell_mangled_does_not_lose_the_message(self):
        """Under a locale the system lacks, /bin/sh glued the first byte of a multibyte character to a `$VAR` before it:
        the argument came in as bytes that are not UTF-8 (Python keeps them as lone surrogates), and every command with
        a text failed on writing the inbox with UnicodeEncodeError. The bad byte becomes U+FFFD, the words stay."""
        bad = "came through: \udc80\udc94 check the line"   # what is left of "— " after the shell ate its first byte
        good = "came through: \ufffd\ufffd check the line"
        for args, kind, field in ((("send", "b", bad), "message.received", "text"),
                                  (("session", "wake", "b", "--about", "d1", bad), "message.received", "text"),
                                  (("task", "new", "b", bad), "delegation.created", "context"),
                                  (("decided", bad), "owner.decided", "text")):
            r = self.hub(*args, me="a")
            self.assertEqual(r.returncode, 0, (args, r.stdout, r.stderr))
            self.assertNotIn("Traceback", r.stderr, args)
            (ev,) = self.spooled()
            self.assertEqual((ev["type"], ev["data"][field]), (kind, good), args)

    @unittest.skipIf(os.name == "nt", "a Windows console is read by its own read_stdin")
    def test_a_byte_that_is_not_utf8_on_stdin_does_not_lose_the_message(self):
        """`hub send b -` under a usual UTF-8 locale (not UTF-8 mode): sys.stdin is strict there, and the command failed
        with UnicodeDecodeError before anything was written. The bad byte becomes U+FFFD, the words stay."""
        env = None
        for name in ("en_US.UTF-8", "C.UTF-8"):   # a locale whose stdin is strict UTF-8 (C.UTF-8 is coerced to lenient)
            e = {**self.env, "HUB_SESSION": "a", "LC_ALL": name, "LANG": name, "PYTHONUTF8": "0",
                 "PYTHONCOERCECLOCALE": "0"}
            probe = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdin.encoding, sys.stdin.errors)"],
                                   env=e, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            if probe.stdout.split() == ["utf-8", "strict"]:
                env = e
                break
        if env is None:
            self.skipTest("no UTF-8 locale with a strict stdin on this machine")
        r = subprocess.run([sys.executable, HUB, "send", "b", "-"], env=env, capture_output=True, timeout=30,
                           input=b"came through: \x80\x94 check the line")
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        (ev,) = self.spooled()
        self.assertEqual(ev["data"]["text"], "came through: \ufffd\ufffd check the line")

    def test_now_rides_with_for_and_about(self):
        """--now in the --for and --about branches is not a word of the text but the message's urgency (the branches used
        not to read it)."""
        for args, key, val in ((("send", "b", "--now", "--for", "m1", "words"), "for", "m1"),
                               (("send", "b", "--for", "m1", "words", "--now"), "for", "m1"),
                               (("send", "b", "--now", "--about", "d1", "words"), "about", "d1"),
                               (("send", "b", "--about", "d1", "words", "--now"), "about", "d1")):
            self.assertEqual(self.hub(*args).returncode, 0, args)
            (ev,) = self.spooled()
            self.assertEqual((ev["data"]["now"], ev["data"][key], ev["data"]["text"]), (True, val, "words"), args)
        self.assertEqual(self.hub("send", "b", "--for", "m1", "words").returncode, 0)
        (ev,) = self.spooled()
        self.assertNotIn("now", ev["data"])

    def test_flags_without_values(self):
        for args in (("session", "new", "x", "--cwd"), ("session", "new", "x", "--mode"),
                     ("session", "new", "x", "--agent", "--about"), ("session", "new", "x", "--icon"),
                     ("session", "role", "a", "agent", "--about"), ("post", "p", "hello", "--reply")):
            self.refused(self.hub(*args))

    def test_a_flag_as_the_last_word_is_a_usage_error(self):
        # a flag without a value went into the text, or the command took a default value and reported "accepted"
        for args, me in ((("task", "new", "b", "fix", "--for"), "a"),
                         (("task", "new", "b", "fix", "--for-session"), "a"),
                         (("task", "new", "b", "fix", "--card"), "a"),
                         (("notify", "hello", "--title"), None), (("wait", "reply", "--for"), "a"),
                         (("send", "b", "hello", "--about"), None), (("send", "b", "hello", "--for"), None),
                         (("send", "b", "--for", "m1", "hello", "--card"), None),
                         (("route", "m1", "executor", "--name"), None),
                         (("task", "go", "x", "--for"), None), (("task", "cancel", "x", "--for"), None),
                         (("tools", "grant", "a", "t", "--for"), None), (("tools", "import", "skill", "x", "--risk"), None),
                         (("tools", "import", "mcp", "x", "--risk", "net", "--about"), None),
                         (("module", "add", "https://example.com/m.git", "--ref"), None)) + tuple(
                ((("task", "run", "x", "do", flag), None)
                 for flag in ("--repo", "--for", "--tools", "--branch", "--model", "--effort", "--work"))):
            r = self.hub(*args, me=me)
            self.refused(r)
            # a session's command answers in English, the owner's terminal in his language (English in tests)
            self.assertIn(i18n.tr("en", "cli.hub.needs_value", flag=args[-1]), r.stderr, args)

    def test_flags_with_values_still_parse(self):
        ok = lambda *a, **k: self.assertEqual(self.hub(*a, **k).returncode, 0, a)  # noqa: E731
        ok("task", "new", "b", "fix", "this", "--for", "m1", "--card", "q2")
        ok("notify", "hello", "--title", "Build", "--urgent")
        ok("send", "b", "--about", "d1", "more", "words")
        ok("task", "run", "x", "do", "this", "--repo", self.state, "--tools", "t1,t2", "--model", "m",
           "--effort", "low")
        ok("tools", "grant", "a", "t", "--for", "m3")
        new, notify, send, run, grant = self.spooled()
        self.assertEqual((new["data"]["for"], new["data"]["card"], new["data"]["context"]), ("m1", "q2", "fix this"))
        self.assertEqual((notify["data"]["title"], notify["data"]["text"], notify["data"]["urgent"]),
                         ("Build", "hello", True))
        self.assertEqual((send["data"]["about"], send["data"]["text"]), ("d1", "more words"))
        self.assertEqual((run["data"]["task"], run["data"]["repo"], run["data"]["tools"], run["data"]["model"]),
                         ("do this", self.state, ["t1", "t2"], "m"))
        self.assertEqual(grant["data"]["for"], "m3")

    def test_tools_grant_with_stray_words_is_a_usage_error(self):
        # extra words silently dropped, and "--for" given with the wrong word passed without the owner's word
        self.refused(self.hub("tools", "grant", "a", "t", "m3"))
        self.refused(self.hub("tools", "grant", "a", "t", "--for", "m3", "more"))

    def test_numbers(self):
        for args in (("away", "--hours", "abc"), ("away", "--hours", "0"), ("away", "--hours", "-1"),
                     ("away", "--hours", "nan"), ("away", "--hours", "inf"), ("away", "--hours"),
                     ("feed", "p", "--last", "0"), ("feed", "p", "--last", "abc"), ("show", "a", "-n", "abc"),
                     ("show", "a", "-n", "0")):
            r = self.hub(*args)
            self.refused(r)
            self.assertTrue(r.stderr.strip(), args)

    def test_web_port(self):
        for cmd in (("secret", "list"), ("ui",)):
            r = self.hub(*cmd, HUB_WEB_PORT="abc")
            self.assertNotEqual(r.returncode, 0, cmd)
            self.assertNotIn("Traceback", r.stderr, cmd)
            self.assertIn("HUB_WEB_PORT", r.stderr, cmd)

    def test_good_values_still_work(self):
        self.assertEqual(self.hub("away", "--hours", "1.5").returncode, 0)
        (ev,) = self.spooled()
        self.assertEqual(ev["data"]["hours"], 1.5)
        self.assertEqual(self.hub("send", "a", "--now", "hello").returncode, 0)
        (ev,) = self.spooled()
        self.assertEqual((ev["data"]["text"], ev["data"]["now"]), ("hello", True))
        self.assertEqual(self.hub("post", "p", "hello", "--reply", "p1").returncode, 0)
        (ev,) = self.spooled()
        self.assertEqual((ev["data"]["text"], ev["data"]["reply_to"]), ("hello", "p1"))


class TextFromStdin(Base):
    """A text in the argument goes through the shell, which runs backticks and eats `$...`; `-` (stdin) and `--file PATH`
    take the text as typed. The three commands that carry a free text: hub send, hub task run, hub task note."""

    RAW = "check `echo HACK` costs $0,17 and $(id) \\n stays\n  indented line\n"

    def setUp(self):
        super().setUp()
        ev = self.j.append("delegation.created", None, {"session": "b", "for_session": "a", "context": "x",
                                                         "channel": "session", "owner": False, "sender": "a"})
        self.did = f"d{ev['seq']}"
        self.path = os.path.join(self.state, "text.txt")
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.RAW)
        self.core()

    def test_send_takes_the_text_from_stdin_as_typed(self):
        r = self.hub("send", "b", "-", me="a", stdin=self.RAW, HUB_CLI_WAIT="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (ev,) = [e for e in journal.read(os.path.join(self.state, "journal.jsonl")) if e["type"] == "message.received"]
        self.assertEqual(ev["data"]["text"], self.RAW.strip())

    def test_send_takes_the_text_from_a_file_and_keeps_its_flags(self):
        r = self.hub("send", "b", "--fyi", "--file", self.path, me="a", HUB_CLI_WAIT="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (ev,) = [e for e in journal.read(os.path.join(self.state, "journal.jsonl")) if e["type"] == "message.received"]
        self.assertEqual((ev["data"]["text"], ev["data"].get("fyi")), (self.RAW.strip(), True))
        r = self.hub("send", "b", "--about", self.did, "-", me="a", stdin="addition $HOME", HUB_CLI_WAIT="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = [e for e in journal.read(os.path.join(self.state, "journal.jsonl")) if e["type"] == "message.received"][-1]
        self.assertEqual((last["data"]["text"], last["data"].get("about")), ("addition $HOME", self.did))

    def test_file_dash_is_stdin_for_send_task_run_and_task_note(self):
        """`--file -` is the same as a bare `-`: the text comes from stdin, not from a file named `-`."""
        def events(kind):
            return [e for e in journal.read(os.path.join(self.state, "journal.jsonl")) if e["type"] == kind]
        r = self.hub("send", "b", "--file", "-", me="a", stdin=self.RAW, HUB_CLI_WAIT="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(events("message.received")[-1]["data"]["text"], self.RAW.strip())
        r = self.hub("task", "run", "x1", "--file", "-", "--work", "normal", "--repo", self.state, me="a", stdin=self.RAW,
                     HUB_CLI_WAIT="5")
        self.assertEqual(events("task.run_requested")[-1]["data"]["task"], self.RAW.strip(), r.stdout + r.stderr)
        r = self.hub("task", "note", self.did, "--file", "-", me="a", stdin="note $HOME", HUB_CLI_WAIT="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.hub("send", "b", "--file", "-", me="a", stdin="  \n")   # empty stdin is refused with a line about stdin
        self.refused(r)
        self.assertNotIn("cannot read -", r.stdout + r.stderr)

    def test_an_empty_stdin_or_file_and_a_bad_file_are_refused_with_a_line(self):
        empty = os.path.join(self.state, "empty.txt")
        with open(empty, "w", encoding="utf-8") as f:
            f.write(" \n\n")
        for args, stdin in ((("send", "b", "-"), ""), (("send", "b", "-"), "  \n"), (("send", "b", "--file", empty), None),
                            (("send", "b", "--file", os.path.join(self.state, "nope.txt")), None),
                            (("send", "b", "--file"), None), (("send", "b", "--file", self.path, "and more"), None),
                            (("task", "run", "x1", "-", "--work", "normal"), ""),
                            (("task", "run", "x1", "--file", empty, "--work", "normal"), None),
                            (("task", "note", self.did, "-"), ""), (("task", "note", self.did, "--file", empty), None)):
            r = self.hub(*args, me="a", stdin=stdin)
            self.refused(r)
            self.assertTrue(r.stdout + r.stderr, args)

    def test_the_first_line_keeps_its_indentation_and_blank_edges_go(self):
        r = self.hub("send", "b", "-", me="a", stdin="\n\n    - item\n      more\n\n", HUB_CLI_WAIT="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (ev,) = [e for e in journal.read(os.path.join(self.state, "journal.jsonl")) if e["type"] == "message.received"]
        self.assertEqual(ev["data"]["text"], "    - item\n      more")

    def test_a_dash_or_file_word_inside_a_longer_text_stays_text(self):
        for words in (["use", "-", "for", "stdin"], ["see", "--file", "x"]):
            r = self.hub("send", "b", *words, me="a", HUB_CLI_WAIT="5")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            last = [e for e in journal.read(os.path.join(self.state, "journal.jsonl"))
                    if e["type"] == "message.received"][-1]
            self.assertEqual(last["data"]["text"], " ".join(words))

    def test_a_request_to_the_liaison_takes_the_text_from_stdin_and_a_file(self):
        """`hub send liaison --to <contact> -` and `--file PATH` (the bus branch of hub send) reach the liaison as the text,
        not as the word `-` or `--file PATH`; the flags stay out of the text; an empty input is refused with a line."""
        os.makedirs(os.path.join(self.state, "bus"))
        with open(os.path.join(self.state, "bus", "view.json"), "w", encoding="utf-8") as f:
            json.dump({"enabled": True, "contacts": {"bee": {"status": "known"}}}, f)
        empty = os.path.join(self.state, "empty.txt")
        with open(empty, "w", encoding="utf-8") as f:
            f.write(" \n")
        for args, stdin in ((("-",), self.RAW), (("--file", self.path), None), (("--now", "-"), self.RAW)):
            r = self.hub("send", "liaison", "--to", "bee", *args, me="a", stdin=stdin, HUB_CLI_WAIT="5")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            last = [e for e in journal.read(os.path.join(self.state, "journal.jsonl"))
                    if e["type"] == "message.received"][-1]
            self.assertEqual((last["data"]["text"], last["data"]["bus_to"]), (self.RAW.strip(), "bee"), args)
        for args, stdin in ((("-",), ""), (("--file", empty), None)):
            r = self.hub("send", "liaison", "--to", "bee", *args, me="a", stdin=stdin)
            self.refused(r)
            self.assertTrue(r.stdout + r.stderr, args)
        r = self.hub("send", "nobody", "--to", "bee", "-", me="a", stdin=self.RAW)   # a wrong addressee: the usage, not the text
        self.refused(r)
        self.assertIn("--to", r.stdout + r.stderr)

    def test_task_run_takes_the_text_from_stdin_and_a_file(self):
        for args, stdin in ((("-",), self.RAW), (("--file", self.path), None)):
            r = self.hub("task", "run", "x1", *args, "--work", "normal", "--repo", self.state, me="a", stdin=stdin,
                         HUB_CLI_WAIT="5")
            ev = [e for e in journal.read(os.path.join(self.state, "journal.jsonl")) if e["type"] == "task.run_requested"][-1]
            self.assertEqual(ev["data"]["task"], self.RAW.strip(), r.stdout + r.stderr)

    def test_task_note_takes_the_text_from_stdin_and_a_file(self):
        for args, stdin in ((("-",), self.RAW), (("--file", self.path), None)):
            r = self.hub("task", "note", self.did, *args, me="a", stdin=stdin, HUB_CLI_WAIT="5")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.state, "tasks", f"{self.did}.md"), encoding="utf-8") as f:
            record = f.read()
        self.assertEqual(record.count("check `echo HACK` costs $0,17 and $(id)"), 2, record)

    def facts(self, kind):
        return [e for e in journal.read(os.path.join(self.state, "journal.jsonl")) if e["type"] == kind]

    def test_task_new_takes_the_text_from_stdin_and_a_file_and_keeps_its_flags(self):
        for args, stdin in ((("-",), self.RAW), (("--file", self.path), None), (("--card", "q5", "-"), self.RAW)):
            r = self.hub("task", "new", "x1", *args, me="a", stdin=stdin, HUB_CLI_WAIT="5")
            ev = self.facts("delegation.created")[-1]
            self.assertEqual(ev["data"]["context"], self.RAW.strip(), r.stdout + r.stderr)
        self.assertEqual(ev["data"].get("card"), "q5")

    def test_the_short_text_commands_take_stdin_and_a_file_as_typed(self):
        for args, kind, field in ((("waiting", "-"), "session.waiting", "text"), (("gist", "-"), "session.gist", "text"),
                                  (("decided", "-"), "owner.decided", "text"),
                                  (("close-ready", "-"), "session.close_ready", "summary")):
            for tail, stdin in (((), self.RAW), (("--file", self.path), None)):
                r = self.hub(*args[:-1], *(tail or args[-1:]), me="a", stdin=stdin, HUB_CLI_WAIT="5")
                got = self.facts(kind)[-1]["data"][field]
                self.assertEqual(got.strip(), self.RAW.strip(), (args, tail, r.stdout + r.stderr))


class Wait(Base):
    """`hub wait` checks are found by what the job is and whose it is, not by the start of its name: after a session
    is renamed, the start of the name is the old one."""

    def test_done_after_rename_removes_own_checks_only(self):
        prompt = "[waiting for reply] vendor reply. Check now"
        self.j.append("schedule.set", "a-wait-ab12-1", {"session": "b", "at": time.time() + 600, "prompt": prompt, **OWN})
        self.j.append("schedule.set", "b-wait-cd34-1", {"session": "a", "at": time.time() + 600, "prompt": prompt, **OWN})
        self.j.append("schedule.set", "b-report", {"session": "b", "at": time.time() + 600, "prompt": "report", **OWN})
        r = self.hub("wait", "done", me="b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(e["type"], e["entity"]) for e in self.spooled()],
                         [("schedule.remove_requested", "a-wait-ab12-1")])

    def test_done_with_a_label_removes_only_that_wait(self):
        # one session may wait for several things; `hub wait done <label>` ends one of them: the label is the tag of
        # the wait (its id) or a piece of what it waits for; no label ends them all, as before
        def arm(jid, what):
            prompt = f"[waiting for reply] {what}. Check now"
            self.j.append("schedule.set", jid, {"session": "b", "at": time.time() + 600, "prompt": prompt,
                                                "wait": what, **OWN})
        arm("b-wait-ab12-1", "reply from Sam in the releases chat")
        arm("b-wait-ab12-2", "reply from Sam in the releases chat")
        arm("b-wait-cd34-1", "review of MR !12 in GitLab")
        r = self.hub("wait", "done", "MR !12", me="b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([e["entity"] for e in self.spooled()], ["b-wait-cd34-1"])
        self.assertIn("1 checks", r.stdout)
        r = self.hub("wait", "done", "ab12", me="b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(e["entity"] for e in self.spooled()), ["b-wait-ab12-1", "b-wait-ab12-2"])

    def test_done_with_a_word_two_waits_share_removes_nothing_and_a_gone_tag_is_not_an_error(self):
        def arm(jid, what):
            self.j.append("schedule.set", jid, {"session": "b", "at": time.time() + 600, wait_key: what, **OWN,
                                                "prompt": f"[waiting for reply] {what}. Check now"})
        wait_key = "wait"
        arm("b-wait-ab12-1", "answer from Anna in the MR")
        arm("b-wait-cd34-1", "answer from Bob")
        r = self.hub("wait", "done", "answer", me="b")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("several", r.stderr)
        self.assertEqual(self.spooled(), [])
        # the tag a fired check names: its wait is already gone (the last check is not active while it runs) - not an error
        r = self.hub("wait", "done", "beef", me="b")
        self.assertEqual((r.returncode, self.spooled()), (0, []), r.stderr)
        self.assertIn("0 checks", r.stdout)

    def test_done_with_a_label_that_matches_nothing_removes_nothing(self):
        prompt = "[waiting for reply] vendor reply. Check now"
        self.j.append("schedule.set", "b-wait-ab12-1", {"session": "b", "at": time.time() + 600, "prompt": prompt,
                                                         "wait": "vendor reply", **OWN})
        r = self.hub("wait", "done", "no such wait", me="b")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("vendor reply", r.stderr)   # what is still being waited for
        self.assertEqual(self.spooled(), [])

    def test_done_keeps_a_watcher_check_with_a_wait_shaped_id(self):
        # a watcher job (`hub schedule add --check`) may take an id of the same shape; `hub wait done` is for the
        # checks `hub wait` itself set, so the watcher keeps running (old `hub wait` jobs have no `wait` field)
        prompt = "[waiting for reply] vendor reply. Check now"
        self.j.append("schedule.set", "b-wait-ab12-1", {"session": "b", "at": time.time() + 600, "prompt": prompt, **OWN})
        self.j.append("schedule.set", "b-wait-cd34-1", {"session": "b", "every": "10m", "prompt": prompt,
                                                         "check": "py watcher.py", **OWN})
        self.j.append("schedule.set", "b-wait-ef56-1", {"session": "b", "every": "10m", "prompt": prompt, **OWN})
        r = self.hub("wait", "done", me="b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(e["type"], e["entity"]) for e in self.spooled()],
                         [("schedule.remove_requested", "b-wait-ab12-1")])
        self.assertIn("1 checks", r.stdout)

    def test_horizon_without_checks_is_an_error(self):
        for limit in ("0m", "0h"):
            self.refused(self.hub("wait", "vendor reply", "--for", limit, me="a"))


class Waiting(Base):
    """The "nothing to check with" hint reads whole words: "github" is not the hub, and a session with a dot in its
    name is a session."""

    HINT = "nobody will catch the reply"   # a session's command: in English (cli.waiting.nothing_checks)

    def test_waiting_hint_knows_dotted_names_and_whole_words(self):
        self.j.append("session.requested", "web.app", {"cwd": self.state, "session_id": "s-w", **OWN})
        # the owner and the hub in another language: by the word stems of the language data
        stems = tuple((stem, False) for stem in langdata.words("words", "people_stems"))
        self.assertTrue(stems)
        for text, shown in (("reply from web.app.", False), ("import from HubSpot", True), ("review on github", True),
                            *stems, ("the hub's go-ahead", False), ("the owner's go-ahead", False),
                            ("owner approval", False), ("waiting for the ownership record", True),
                            ("reply from hubs", False), ("reply from session b", False), ("vendor reply", True)):
            r = self.hub("waiting", text, me="a")
            self.assertEqual(self.HINT in r.stdout, shown, text)

    def test_each_language_with_data_is_known_by_its_own_stems(self):
        # the stems are data of the language, not code: the Ukrainian ones are in i18n/data/uk.json
        stems = langdata.get("uk", "words", "people_stems")
        self.assertTrue(stems)
        for stem in stems:
            self.assertNotIn(self.HINT, self.hub("waiting", stem, me="a").stdout, stem)

    def test_a_command_handed_to_her_is_no_check(self):
        # a command handed to it (retarget writes session) is its own, but the clock runs it without a session: it
        # will not catch the answer
        self.j.append("schedule.set", "mon", {"every": "1h", "command": "true", **OWN})
        self.j.append("schedule.retarget", "mon", {"session": "a", **OWN})
        self.assertIn(self.HINT, self.hub("waiting", "vendor reply", me="a").stdout)
        self.j.append("schedule.set", "a-chk", {"session": "a", "every": "1h", "prompt": "check", **OWN})
        self.assertNotIn(self.HINT, self.hub("waiting", "vendor reply", me="a").stdout)


class ScheduleList(Base):
    """`hub schedule list` names who set a job in the reader's language: the state keeps the word of the core that wrote it."""

    def test_who_set_a_job_is_read_from_the_catalog(self):
        self.j.append("schedule.set", "mon", {"every": "1h", "command": "true", **OWN})
        self.j.append("schedule.set", "chk", {"every": "1h", "session": "a", "prompt": "check", "channel": "session",
                                              "sender": "b"})
        for lang in ("en", "ru"):
            out = self.hub("schedule", "list", HUB_LANG=lang).stdout
            self.assertEqual([line.split("(", 1)[1].split(",")[0] for line in out.splitlines() if "\u2192" in line],
                             [i18n.tr(lang, "cli.schedule.list_row", id="", to="", when="", state="", by=b, runs=0, what="")
                              .split("(", 1)[1].split(",")[0] for b in ("b", i18n.tr(lang, "cli.show.owner"))], lang)
        self.assertFalse(re.search("[\u0400-\u04ff]", self.hub("schedule", "list", HUB_LANG="en").stdout))


class McpLogin(Base):
    """--no-browser and --redirect together are options, not a reason to show the help."""

    def test_both_options_parse(self):
        r = self.hub("mcp", "login", "ghost", "srv", "--no-browser", "--redirect", "http://localhost:1/cb?code=x")
        self.assertIn("no session ghost", r.stderr)
        r = self.hub("mcp", "login", "ghost", "srv", "--redirect", "http://localhost:1/cb?code=x", "--no-browser")
        self.assertIn("no session ghost", r.stderr)


class Texts(unittest.TestCase):
    """The help names what exists: no forks, and all the restart parts are listed."""

    def doc(self):
        from core import clihelp
        with open(clihelp.HELP, encoding="utf-8") as f:
            return f.read()

    def test_help_has_no_forks(self):
        self.assertNotIn("hub session fork", self.doc())

    def test_help_lists_every_part(self):
        doc = self.doc()
        listed = set(doc.split("hub restart <part…>")[1].split("--now  ")[0].replace(",", " ").split())
        self.assertEqual([p for p in restart.PARTS if p not in listed], [])


if __name__ == "__main__":
    unittest.main()
