"""Help of the hub commands (core/hubhelp.txt, core/clihelp.py): one description for `hub --help`, `hub <command>
--help` and the flags bin/hub accepts.

Measured 05.11 on 7 days of hub-session transcripts: `hub <command> --help` printed the whole 40 KB help, sessions cut
it with `| head -30` and in 129 of 256 calls the command they asked about was not in what they read; 96 times they
asked for help again within three steps. The checks below hold the description to the code: every flag the code
reads is described and every described flag is read; every subcommand has its help; `--help` sends nothing; an
unknown flag is refused instead of going into the text; who may run a command is what the core really lets through."""
import tests  # noqa: F401 — ignore the machine's settings
import ast
import concurrent.futures
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

import platforms
from core import journal, clihelp, cutout, reducer, spool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
FLAG = clihelp.FLAG
NOT_FLAGS = set(clihelp.HELP_WORDS)

# commands whose parsing lives in another file: (file, the functions that parse its words — only their own flags, not
# what they call: that builds other programs' command lines), and flags passed on to another program (claude, tmux)
ELSEWHERE = {
    "shot": [("modules/proof/shot.py", ("parse",))],
    "video": [("modules/proof/video.py", ("main",))],
    "critic": [("modules/critic/critic.py", ("main", "parse"))],
    "contacts": [("modules/contacts/contacts.py", ("main",))],
    "chats": [("core/chatscli.py", ("main",))],
    "market": [("modules/market/market.py", ("main",))],
    "secret": [("modules/secrets/cli.py", ("main", "parse", "parse_add"))],
    "remote": [("modules/cloudflare/cli.py", ("parser", "main")), ("modules/cloudflare/setup.py", ("parser", "main"))],
    "setup": [("core/ownclaude.py", ("main",))],
    "support-bundle": [("core/supportbundle.py", ("main",))],
    "uninstall": [("core/uninstall.py", ("main",))],
    "instance": [("core/instancecli.py", ("main", "new", "show", "remove"))],
    "marketplace": [("core/marketplace.py", ("main",))],
    "direction": [("modules/directions/direction.py", ("main", "board_cmd", "decide"))],
}
PASSED_ON = {"send-file": {"--title"}, "send-rich": {"--screenshot"}, "setup": {"--version"}, "attach": {"-t"}, "take": {"--permission-mode", "--resume", "--settings"}}
# commands with their own help text next to their parsing (the `>` line): its flags must be in that text
OWN = {"accounts": "modules/accounts/accounts.py", "conduit": "modules/conduit/conduit.py",
       "stats": "modules/stats/stats.py", "codex": "core/codexcli.py"}
# the open flavor cuts these commands' code out of the tree (core/cutout.py, bin/hub CUT_COMMANDS): no help of the core
CUT = {c for c, f in {"accounts": "profile", "conduit": "conduit", "stats": "stats"}.items() if not cutout.present(f)}
OWN = {c: p for c, p in OWN.items() if c not in CUT}


def flags_in(node):
    pieces = {id(v) for n in ast.walk(node) if isinstance(n, ast.JoinedStr) for v in n.values}   # f"{x}-wait-" parts
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in pieces and FLAG.fullmatch(n.value)} - NOT_FLAGS


def calls_in(node):
    return {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def reach(funcs, node, seen=None):
    """Flags in node and in the functions of the same file it calls, transitively."""
    seen = set() if seen is None else seen
    out = flags_in(node)
    for name in calls_in(node) - seen - {"main"}:   # main is the dispatcher itself (or another module's, imported)
        if name in funcs:
            seen.add(name)
            out |= reach(funcs, funcs[name], seen)
    return out


def parse_file(path):
    from core import embed
    with open(embed.where(path, REPO) or os.path.join(REPO, path), encoding="utf-8") as f:   # the core's own or an embed's
        tree = ast.parse(f.read())
    funcs = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(n.name, n)
    return tree, funcs


def cmds_of(test):
    """The command names an `if` of main() tests: `cmd == "x"`, `cmd in ("a", "b")`."""
    out = set()
    for n in ast.walk(test):
        if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "cmd":
            for op, right in zip(n.ops, n.comparators):
                if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
                    out.add(right.value)
                elif isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.List)):
                    out |= {e.value for e in right.elts if isinstance(e, ast.Constant)}
    return out


def branches():
    """(command names, the if node) of every branch of bin/hub main() that names a command."""
    tree, funcs = parse_file("bin/hub")
    out = []

    def walk(stmts):
        for s in stmts:
            if isinstance(s, ast.If):
                names = cmds_of(s.test)
                if names:
                    out.append((names, s))
                walk(s.orelse)
    walk(funcs["main"].body)
    return out, funcs


def code_flags():
    """{command: flags its code reads}: the branches of main() and what they call in bin/hub, and for a command
    parsed elsewhere — the functions named in ELSEWHERE."""
    found, funcs = branches()
    out = {}
    for names, node in found:
        flags = reach(funcs, node.test) | set().union(*(reach(funcs, s) for s in node.body))
        for name in names:
            out.setdefault(name, set()).update(flags - PASSED_ON.get(name, set()))
    for cmd, places in ELSEWHERE.items():
        for path, names in places:
            _, mfuncs = parse_file(path)
            for name in names:
                out.setdefault(cmd, set()).update(flags_in(mfuncs[name]) - PASSED_ON.get(cmd, set()))
    from modules.directions import direction   # decide reads "--" + a section name
    out["direction"] |= {"--" + s for s in direction.SECTIONS}
    return out


def sub_flags():
    """{(command, subcommand): flags} the code reads only for that subcommand: branches of main() that test
    `rest[:1] == ["x"]` (or a tuple of them), and `if sub == "x"` blocks of the helpers a command calls."""
    found, funcs = branches()
    out = {}

    def subs_of(test, left_name):
        got = set()
        for n in ast.walk(test):
            if isinstance(n, ast.Compare) and ast.unparse(n.left) == left_name:
                for op, right in zip(n.ops, n.comparators):
                    if isinstance(op, ast.Eq) and isinstance(right, ast.List):
                        got |= {e.value for e in right.elts if isinstance(e, ast.Constant)}
                    elif isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
                        got.add(right.value)
                    elif isinstance(op, ast.In) and isinstance(right, ast.Tuple):
                        got |= {e.elts[0].value for e in right.elts if isinstance(e, ast.List) and e.elts}
        return {g for g in got if isinstance(g, str) and not g.startswith("-")}
    for names, node in found:
        subs = subs_of(node.test, "rest[:1]")
        if subs:
            flags = reach(funcs, node.test) | set().union(*(reach(funcs, b) for b in node.body))
            for name in names:
                out[(name, tuple(sorted(subs)))] = flags - PASSED_ON.get(name, set())
    for cmd, helper in (("schedule", "schedule_cmd"), ("tools", "tools_cmd")):
        for n in ast.walk(funcs[helper]):
            if isinstance(n, ast.If):
                subs = subs_of(n.test, "sub")
                if subs:
                    flags = reach(funcs, n.test) | set().union(*(reach(funcs, b) for b in n.body))
                    out[(cmd, tuple(sorted(subs)))] = flags
    return out


def subcommands():
    """(command, subcommand) pairs the code dispatches on: `rest[:1] == ["x"]`, `rest[:1] in (["a"], ["b"])` in main(),
    and `sub == "x"` in the helpers a command calls."""
    found, funcs = branches()
    out = set()

    def firsts(test):
        got = set()
        for n in ast.walk(test):
            if not isinstance(n, ast.Compare):
                continue
            left = ast.unparse(n.left)
            for op, right in zip(n.ops, n.comparators):
                if left in ("rest[:1]",) and isinstance(op, ast.Eq) and isinstance(right, ast.List):
                    got |= {e.value for e in right.elts if isinstance(e, ast.Constant)}
                elif left == "rest[:1]" and isinstance(op, ast.In) and isinstance(right, ast.Tuple):
                    got |= {e.elts[0].value for e in right.elts if isinstance(e, ast.List) and e.elts}
                elif left == "sub" and isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
                    got.add(right.value)
        return got
    helpers = {"schedule": "schedule_cmd", "tools": "tools_cmd", "layout": "layout_cmd", "module": "module_cmd",
               "marketplace": "marketplace_cmd", "wait": "wait_cmd"}
    for names, node in found:
        subs = firsts(node.test) | set().union(*(firsts(s) for s in node.body))
        for name in names:
            out |= {(name, s) for s in subs if isinstance(s, str) and not s.startswith("-")}
    for cmd, helper in helpers.items():
        out |= {(cmd, s) for s in firsts(funcs[helper]) if isinstance(s, str)}
    return out


def run_hub(*args, env=None, state=None):
    state = state or tempfile.mkdtemp(prefix="clihelp-")
    e = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "PYTHONUTF8": "1", **(env or {})}
    if "HUB_SESSION" not in (env or {}):
        e.pop("HUB_SESSION", None)
    r = subprocess.run([sys.executable, HUB, *args], env=e, capture_output=True, text=True, encoding="utf-8",
                       stdin=subprocess.DEVNULL, timeout=60)
    return r, [ev for _, ev in spool.take(os.path.join(state, "spool"))]


class TheFile(unittest.TestCase):
    def setUp(self):
        self.h = clihelp.load()

    def test_every_command_has_a_summary_forms_and_who(self):
        for e in self.h.entries:
            self.assertTrue(e.summary and e.forms, e.key)
            self.assertTrue(e.who or e.own, f"{e.key}: no «@ who» line")
            self.assertEqual([r for r in e.roles() if r not in clihelp.WHO], [], e.key)

    def test_flags_named_in_a_form_are_described(self):
        for e in self.h.entries:
            self.assertEqual(sorted(e.form_flags() - e.flag_names()), [], e.key)

    def test_examples_are_commands_the_parser_accepts(self):
        for e in self.h.entries:
            for x in e.examples:
                words = shlex.split(x)
                self.assertEqual(words[0], "hub", x)
                exact, subs, _ = self.h.find(words[1:])
                self.assertIsNotNone(exact, x)
                self.assertEqual(self.h.unknown(words[1], words[2:]), [], x)

    def test_examples_use_made_up_names(self):
        live = re.compile(r"/Users/|/home/[a-z]{2,}/|\b[A-Z]{2,}-\d+\b")
        for e in self.h.entries:
            for x in e.examples:
                self.assertIsNone(live.search(x), x)

    def test_a_broken_line_names_its_number(self):
        with self.assertRaisesRegex(clihelp.Bad, "^3:"):
            clihelp.Help("= G\nhub x\n--flag-without-description\n")
        with self.assertRaisesRegex(clihelp.Bad, "twice"):
            clihelp.Help("= G\nhub x\n: one\n\nhub x\n: two\n")

    def test_the_index_is_short_and_names_every_command(self):
        index = self.h.index()
        self.assertLess(len(index), 10_000)   # it was 40 KB: sessions read it through `| head`
        for e in self.h.entries:
            self.assertIn(f"  {e.key} ", index)

    def test_hub_update_is_described_as_the_install_it_is(self):
        """`hub update` with no word installs (the INSTALL_FLAG, as the page's "Update" button): the help once called it
        "check for an update now" and a person ran it to look."""
        e = self.h.find(["update"])[0]
        self.assertNotIn("check for", e.summary)
        self.assertIn("install", e.summary)
        self.assertIn("restart", e.summary)

    def test_the_text_of_send_task_run_and_task_note_can_come_from_stdin_or_a_file(self):
        for words in (["send"], ["task", "run"], ["task", "note"]):
            text = self.h.render(words)
            self.assertIn("--file PATH", text, words)
            self.assertIn("<<'EOF'", text, words)
        self.assertEqual(self.h.unknown("send", ["maps", "--file", "/tmp/x.txt"]), [])
        self.assertEqual(self.h.unknown("task", ["note", "--file", "/tmp/x.txt"]), [])

    def test_the_model_rule_goes_under_task_run(self):
        text = self.h.render(["task", "run"], rule=lambda: "THE RULE")
        self.assertIn("  THE RULE", text)
        self.assertNotIn(clihelp.RULE, text)


class AgainstTheCode(unittest.TestCase):
    """The description and the parsing are one set of flags: nothing the code reads is missing from the help, nothing
    the help names is not read (bin/hub refuses a flag the help does not have)."""

    def test_every_flag_the_code_reads_is_described_and_back(self):
        h = clihelp.load()
        got = code_flags()
        for top in sorted(h.tops()):
            if top in OWN or top == "bus":
                continue
            described = h.flags(top)[0]
            self.assertEqual((top, sorted(got.get(top, set()) - described)), (top, []), "read but not described")
            self.assertEqual((top, sorted(described - got.get(top, set()))), (top, []), "described but not read")

    def test_a_subcommand_is_refused_only_flags_its_code_does_not_read(self):
        # bin/hub checks the flags of the subcommand the words name (`task note` has no --repo of `task run`): every
        # flag a subcommand's own branch reads is in that subcommand's help, or a real flag would be refused
        h = clihelp.load()
        missing = []
        for (cmd, subs), flags in sorted(sub_flags().items()):
            entries = [e for e in h.entries if e.key in {f"{cmd} {x}" for x in subs}]
            if not entries:
                continue
            described = set().union(*(e.flag_names() for e in entries))
            missing += [f"{cmd} {'|'.join(subs)}: {f}" for f in sorted(flags - described)]
        self.assertEqual(missing, [])

    def test_argparse_commands_are_parsed_by_argparse(self):
        # the leniency of `% argparse` (a start of a flag, --name=value) only where the parser really is argparse
        h = clihelp.load()
        tops = h.labels["argparse"].split()
        self.assertTrue(tops)
        sources = {"schedule": "bin/hub", "remote": "modules/cloudflare/cli.py"}
        for top in tops:
            with open(os.path.join(REPO, sources[top]), encoding="utf-8") as f:
                self.assertIn("ArgumentParser", f.read(), top)

    def test_commands_with_several_entries_have_their_subcommands_checked(self):
        # bin/hub narrows the flags to a subcommand's own entry only for these: each must be dispatched where
        # sub_flags() looks (main() by rest[:1], schedule_cmd and tools_cmd by sub), or the narrowing is unchecked
        h = clihelp.load()
        several = {t for t in h.tops() if len([e for e in h.of_top(t) if " " in e.key]) >= 2} - {"direction"}
        covered = {cmd for cmd, _ in sub_flags()}
        self.assertEqual(sorted(several - covered), [])

    def test_every_command_the_code_knows_is_described(self):
        h = clihelp.load()
        found, _ = branches()
        gone = {"delegate", "delegations", "delegation"}   # old words: they only say what to use instead
        named = set().union(*(names for names, _ in found)) - gone
        self.assertEqual(sorted(named - h.tops() - CUT), [])

    def test_every_subcommand_the_code_dispatches_has_its_help(self):
        h = clihelp.load()
        missing = []
        # fork only says what to use instead; watch and token are the parts' own processes (bin/hub-part conduit, the
        # conduit token helper), not commands for a person or a session
        internal = {("session", "fork"), ("conduit", "watch"), ("conduit", "token"), ("stats", "watch")}
        for cmd, sub in sorted(s for s in subcommands() - internal if s[0] not in CUT):
            if cmd in OWN:   # the command's own help text names its subcommands
                if not re.search(rf"(?<![\w-]){re.escape(sub)}(?![\w-])", clihelp.own_text(h.of_top(cmd)[0].own)):
                    missing.append(f"{cmd} {sub}")
                continue
            exact, subs, key = h.find([cmd, sub])
            entries = [exact] if exact else []
            entries += subs
            if not any(re.search(rf"(?<![\w-]){re.escape(sub)}(?![\w-])", " ".join(e.forms)) for e in entries):
                missing.append(f"{cmd} {sub}")
        self.assertEqual(missing, [])

    def test_an_own_help_named_by_its_catalog_key_prints_the_line(self):
        # core.codexcli:USAGE holds the key of its line: `hub codex --help` printed "cli.codex.usage"
        text = clihelp.own_text("core.codexcli:USAGE")
        self.assertNotIn("cli.codex.usage", text)
        self.assertIn("logout", text)

    def test_commands_with_their_own_help_have_its_flags_in_it(self):
        h = clihelp.load()
        for top, path in OWN.items():
            entry = h.of_top(top)[0]
            text = clihelp.own_text(entry.own)
            _, funcs = parse_file(path)
            # accounts: the former spelling, dropped silently; stats: the child of its report (bin/hub-part stats --report)
            flags = flags_in(funcs["main"]) - {"--token-stdin", "--report"}
            self.assertEqual(sorted(f for f in flags if f not in text), [], top)


class FromTheCommandLine(unittest.TestCase):
    def test_help_of_every_command_prints_it_and_sends_nothing(self):
        h = clihelp.load()

        def one(e):
            r, evs = run_hub(*e.key.split(), "--help")
            return e, r, evs
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            for e, r, evs in pool.map(one, h.entries):
                self.assertEqual((e.key, r.returncode, evs), (e.key, 0, []), r.stderr[-500:])
                self.assertIn(e.forms[0], r.stdout, e.key)
                self.assertLess(len(r.stdout.encode()), 12_000, e.key)

    def test_the_other_ways_to_ask_give_the_same(self):
        want = run_hub("schedule", "add", "--help")[0].stdout
        self.assertIn("--prompt", want)
        for args in (("schedule", "add", "-h"), ("help", "schedule", "add")):
            r, evs = run_hub(*args)
            self.assertEqual((r.stdout, evs), (want, []), args)
        index = run_hub("--help")[0].stdout
        self.assertIn("schedule add", index)
        self.assertNotIn("--prompt", index)
        for args in ((), ("help",), ("-h",)):
            self.assertEqual(run_hub(*args)[0].stdout, index, args)

    def test_the_codex_command_is_in_the_index_only_of_a_hub_on_codex(self):
        # a hub without "host": "codex" sees the index of main; its own help answers on any hub
        claude = tempfile.mkdtemp(prefix="clihelp-")
        self.assertNotRegex(run_hub("--help", env={"HUB_CONFIG": os.path.join(claude, "hub.json")})[0].stdout, r"(?m)^  codex ")
        self.assertIn("hub codex", run_hub("codex", "--help")[0].stdout)
        codex = tempfile.mkdtemp(prefix="clihelp-")
        with open(os.path.join(codex, "hub.json"), "w") as f:
            json.dump({"host": "codex"}, f)
        self.assertRegex(run_hub("--help", env={"HUB_CONFIG": os.path.join(codex, "hub.json")}, state=codex)[0].stdout,
                         r"(?m)^  codex ")

    def test_help_after_a_session_name_is_the_commands_help(self):
        r, evs = run_hub("show", "maps", "--help")
        self.assertEqual(evs, [])
        self.assertIn("hub show --grep", r.stdout)

    def test_a_group_lists_its_subcommands(self):
        out = run_hub("session", "--help")[0].stdout
        self.assertIn("hub session wake <name>", out)
        self.assertIn("hub session <…> --help", out)

    def test_an_unknown_flag_is_refused_and_nothing_is_sent(self):
        # sessions guessed flags: the word went into the text of the message (`--reply-to x` in what maps got)
        r, evs = run_hub("send", "maps", "--reply-to", "m12", "done")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(evs, [])
        self.assertIn("--reply-to", r.stderr)
        self.assertIn("hub send --help", r.stderr)
        self.assertIn("--fyi", r.stderr)   # what the command has
        r, evs = run_hub("shot", "--compare", "a.png", "b.png", "-o", "c.png")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--out", r.stderr)

    def test_the_help_of_send_tells_a_script_outside_a_session_how_not_to_be_the_owner(self):
        # a watchdog run by systemd or cron had no variable, and its text closed the owner's cards as his word
        r, _ = run_hub("send", "--help", env={"HUB_LANG": "en"})
        self.assertIn("HUB_SCHEDULE_JOB=<name>", r.stdout)
        state = tempfile.mkdtemp(prefix="clihelp-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", "channel": "cli", "owner": True})
        j.append("session.started", "maps", {"session_id": "s-m"})
        r, evs = run_hub("send", "maps", "all clear", env={"HUB_SCHEDULE_JOB": "watchdog"}, state=state)
        (ev,) = [e for e in evs if e["type"] == "message.received"]
        self.assertEqual((ev["data"]["owner"], ev["data"]["schedule_job"]), (False, "watchdog"))   # what the help promises
        r, evs = run_hub("send", "maps", "all clear", state=state)
        ev = [e for e in evs if e["type"] == "message.received"][-1]   # the earlier one is still in the mailbox
        self.assertTrue(ev["data"]["owner"])   # and without it, from a terminal, his word

    def test_help_at_the_end_is_help_and_does_nothing(self):
        # review 05.11: `hub session stop maps-1 --help` stopped maps-1 — only the first two words were looked at
        for args in (("session", "stop", "maps-1", "--help"), ("session", "remove", "maps-1", "-h"),
                     ("task", "new", "maps", "--help"), ("send", "maps", "--help"), ("decided", "something", "--help")):
            r, evs = run_hub(*args)
            self.assertEqual((r.returncode, evs), (0, []), args)
            self.assertIn("hub " + " ".join(a for a in args[:2] if a not in ("maps", "something")), r.stdout, args)

    def test_a_flag_of_another_subcommand_or_typed_otherwise_is_refused(self):
        h = clihelp.load()
        self.assertEqual(h.unknown("task", ["new", "maps", "fix", "--repo", "/srv/x"]), ["--repo"])
        self.assertEqual(h.unknown("task", ["run", "maps-1", "fix", "--repo", "/srv/x", "--work", "normal"]), [])
        self.assertEqual(h.unknown("send", ["maps", "--reply-to=m5", "ok"]), ["--reply-to=m5"])
        self.assertEqual(h.unknown("shot", ["--compare", "a.png", "b.png", "-O", "c.png"]), ["-O"])
        self.assertEqual(h.unknown("send", ["maps", "--Now", "ok"]), ["--Now"])
        # argparse takes a unique start of a flag and `--flag=value`: accepted there; the other commands read exact
        # words, so there a start or `=` would be a silent no-flag — refused (review 05.11: `--no` taken for --now)
        self.assertEqual(h.unknown("schedule", ["add", "--sess", "maps", "--prompt=check", "--every", "1h"]), [])
        self.assertEqual(h.unknown("send", ["maps", "--no", "ok"]), ["--no"])
        self.assertEqual(h.unknown("task", ["run", "maps-1", "fix", "--work=normal"]), ["--work=normal"])
        self.assertEqual(h.unknown("send", ["maps", "-Hγειά"]), [])   # not a flag: a word of the text
        self.assertEqual(h.unknown("send", ["maps", "-", "ok"]), [])   # a dash alone is a word
        r, evs = run_hub("task", "new", "maps", "fix", "--repo", "/srv/x")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(evs, [])
        self.assertIn("--repo", r.stderr)

    def test_help_inside_free_text_of_a_subcommand_stays_text(self):
        # review 05.11: help anywhere dropped the note; inside the text a help word is a word, as before
        state = tempfile.mkdtemp(prefix="clihelp-")
        r, evs = run_hub("task", "note", "fixed", "--help", "in", "CLI", env={"HUB_SESSION": "maps"}, state=state)
        self.assertNotIn("hub task note [dN", r.stdout)

    def test_a_value_after_a_flag_is_not_a_flag(self):
        r, evs = run_hub("task", "note", "d1", "--fyi")   # a free-text word that looks like a flag: refused
        self.assertNotEqual(r.returncode, 0)
        r, evs = run_hub("send", "maps", "--about", "d3", "text about --fyi in quotes")
        self.assertNotIn("--fyi", r.stderr)
        self.assertEqual([e["data"]["text"] for e in evs], ["text about --fyi in quotes"])

    def test_help_inside_free_text_stays_text(self):
        (ev,) = run_hub("decided", "showed", "--help", "into the help")[1]
        self.assertEqual(ev["data"]["text"], "showed --help into the help")

    def test_a_wrong_form_prints_that_commands_help_not_the_index(self):
        r, evs = run_hub("schedule", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hub schedule add", r.stderr)
        self.assertNotIn("hub session new", r.stderr)
        self.assertLess(len(r.stderr.encode()), 12_000)

    def test_an_unknown_command_says_so_and_shows_the_index(self):
        r, evs = run_hub("nosuchcommand")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nosuchcommand", r.stderr)
        self.assertIn("schedule add", r.stderr)

    def test_the_index_lists_core_module_commands(self):
        # unchanged: a core module's command is in the index with its module (tests/test_modapi_hooks.py covers it live)
        h = clihelp.load()
        self.assertIn("todo", h.index([("todo", "m", "module chores")]))


OWNER = {"channel": "cli", "owner": True}
HUB_SAYS = {"channel": "session", "owner": False, "sender": "hub"}


def said_by(name):
    return {"channel": "session", "owner": False, "sender": name}


class WhoMay(unittest.TestCase):
    """The `@ who` line of a command is what the core lets through: the same events from the owner, the hub, another
    session (and the session itself, the one who raised it) are accepted or refused as the help says."""

    def fresh(self):
        self.seq = 0
        st = reducer.empty()
        for name, extra in (("hub", {"kind": "hub"}), ("lead", {}), ("other", {}), ("third", {})):
            st = self.ev(st, "session.requested", name, cwd="/w", session_id="s-" + name, **extra, **OWNER)[0]
            st = self.ev(st, "session.started", name)[0]
        # an executor raised by lead (as the core raises it for `hub task run`)
        st = self.ev(st, "session.requested", "maps-1", cwd="/w", session_id="s-x", executor=True, raised_by="lead",
                     **OWNER)[0]
        return self.ev(st, "session.started", "maps-1")[0]

    def ev(self, st, type, entity=None, **data):
        self.seq += 1
        st, _, why = reducer.reduce(st, {"seq": self.seq, "ts": 1_000 + self.seq, "type": type, "entity": entity,
                                         "data": data})
        return st, why

    def allowed(self, key, type, entity, data, prep=None):
        """Who of owner, hub, other, itself, the raiser the core lets send this event."""
        out = set()
        for who, speaker in (("owner", OWNER), ("hub", HUB_SAYS), ("session", said_by("third")),
                             ("self", said_by(entity or "third")), ("raiser", said_by("lead"))):
            st = self.fresh()
            if prep:
                st = prep(self, st)
            _, why = self.ev(st, type, entity, **data, **speaker)
            if why is None:
                out.add(who)
        return out

    def claims(self, key):
        roles = set(clihelp.load().find(key.split())[0].roles())
        return roles

    def check(self, key, type, entity, data, prep=None, ask=("owner", "hub", "session")):
        got = self.allowed(key, type, entity, data, prep) & set(ask)
        roles = self.claims(key)
        want = {r for r in ask if r in roles or "anyone" in roles or (r == "session" and "session" in roles)}
        self.assertEqual((key, sorted(got)), (key, sorted(want)))

    def test_session_commands(self):
        busy = lambda self, st: self.ev(st, "turn.started", "maps-1", marker=None)[0]  # noqa: E731
        self.check("session stop", "session.stop_requested", "maps-1", {}, ask=("owner", "hub", "session", "raiser"))
        self.check("session interrupt", "session.interrupt_requested", "maps-1", {}, prep=busy,
                   ask=("owner", "hub", "session", "raiser"))
        self.check("session effort", "session.effort_set", "other", {"level": "high"}, ask=("owner", "hub", "self"))
        self.check("session model", "session.model_set", "other", {"model": "sonnet"}, ask=("owner", "hub", "self"))

    def test_hub_commands(self):
        self.check("restart", "hub.restart_requested", None, {"parts": [], "now": False, "why": None})
        self.check("away", "owner.away", None, {"hours": None})
        self.check("back", "owner.back", None, {}, prep=lambda self, st: self.ev(st, "owner.away", None, hours=None,
                                                                               **OWNER)[0])

    def test_terminal_only_commands_refuse_a_session(self):
        # bin/hub itself refuses them in a session: the help names the terminal as the place
        h = clihelp.load()
        for key, args in (("stop", ["stop"]), ("migrate", ["migrate", "--dry"]), ("uninstall", ["uninstall", "--dry-run"]),
                          ("support-bundle", ["support-bundle"])):
            self.assertIn("terminal", h.find(key.split())[0].roles(), key)
            r, evs = run_hub(*args, env={"HUB_SESSION": "maps"})
            self.assertNotEqual(r.returncode, 0, key)
            self.assertEqual(evs, [], key)

    def test_session_only_commands_refuse_the_terminal(self):
        h = clihelp.load()
        for key, args in (("wait", ["wait", "a reply in the thread"]), ("slash", ["slash", "/ship"]),
                          ("tools request", ["tools", "request", "browser:mac", "check"])):
            self.assertEqual(h.find(key.split())[0].roles(), ["self"], key)
            r, evs = run_hub(*args)
            self.assertNotEqual(r.returncode, 0, key)
            self.assertEqual(evs, [], key)


class Instructions(unittest.TestCase):
    """Skills and the hub's CLAUDE.md name only commands and flags the CLI has: a stale flag in a skill is a session
    guessing every time."""

    def test_commands_in_the_texts_for_sessions_exist(self):
        h = clihelp.load()
        paths = glob.glob(os.path.join(REPO, "plugins", "**", "SKILL.md"), recursive=True) + \
            glob.glob(os.path.join(REPO, "modules", "*", "plugin", "**", "SKILL.md"), recursive=True) + \
            [os.path.join(REPO, "hub-home", "CLAUDE.md")]
        bad = []
        for path in paths:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for span in re.findall(r"`(hub [^`]+)`", text):
                try:
                    words = shlex.split(span)
                except ValueError:
                    continue
                if len(words) < 2 or not clihelp.WORD.fullmatch(words[1]):
                    continue
                if words[1] not in h.tops():
                    bad.append(f"{os.path.relpath(path, REPO)}: {span} — no such command")
                elif words[1] not in ("direction", "bus", "accounts", "conduit", "stats"):
                    wrong = h.unknown(words[1], [w for w in words[2:] if not re.fullmatch(r"<[^>]*>|…", w)])
                    if wrong:
                        bad.append(f"{os.path.relpath(path, REPO)}: {span} — no such flag: {', '.join(wrong)}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
