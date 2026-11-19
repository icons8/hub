"""Help of the `hub` commands from one description, core/hubhelp.txt: the index (`hub --help`), the help of a command
(`hub <command> --help`) and the flags each command accepts (bin/hub refuses an unknown one; tests/test_clihelp.py
checks the file against what the code really parses and lets through).

Measured before (transcripts of hub sessions, 7 days, 05.11): `hub <command> --help` printed the whole 40 KB help,
sessions cut it with `| head` or `| grep`, and half of the time the command they asked about was not in what they saw.

stdlib only and no hub imports at the top: bin/hub loads it on every help call.
"""
import difflib
import os
import re

HELP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hubhelp.txt")
FLAG = re.compile(r"--?[a-z][a-z0-9-]*")
# what a person or a model may type as a flag: any case, `--name=value`
TYPED = re.compile(r"(--?[A-Za-z][A-Za-z0-9_-]*)(=.*)?", re.S)
IN_FORM = re.compile(r"(?<![\w-])(--?[a-z][a-z0-9-]*)")
WORD = re.compile(r"[a-z][a-z0-9-]*")
HELP_WORDS = ("-h", "--help")
# who may run a command: the words of the `@` line before " — " (tests/test_clihelp.py checks them against the core)
WHO = ("anyone", "owner", "hub", "session", "self", "raiser", "setter", "mini-hub", "agent-dev", "terminal", "job",
       "liaison")
RULE = "{model_rule}"   # the --work rule with this hub's pairs (bin/hub pin_rule)


class Bad(ValueError):
    """The help file breaks its own layout: the line number and what is wrong."""


class Entry:
    def __init__(self, group):
        self.group, self.forms, self.summary, self.details = group, [], "", []
        self.flags, self.who, self.examples, self.errors, self.own = [], "", [], [], None
        self.key = ""

    def flag_names(self):
        return {f for f, _, _ in self.flags}

    def value_flags(self):
        """Flags followed by a value (`--for mN`): the value is not a flag even if it starts with a dash."""
        return {f for f, arg, _ in self.flags if arg}

    def form_flags(self):
        return {m for form in self.forms for m in IN_FORM.findall(form)}

    def roles(self):
        head = self.who.split(" — ")[0]
        return [r.strip() for r in re.split(r"[,;]", head) if r.strip()]


def _key(form):
    words = []
    for w in form.split()[1:]:
        if not WORD.fullmatch(w):
            break
        words.append(w)
    return words


class Help:
    def __init__(self, text):
        self.labels, self.groups, self.entries = {}, [], []
        group, cur = None, None
        for n, raw in enumerate(text.split("\n"), 1):
            line = raw.rstrip()
            if not line or line.startswith("#"):
                cur = None if not line else cur
                continue
            if line.startswith("% "):
                name, _, value = line[2:].partition(" ")
                self.labels[name] = value
                continue
            if line.startswith("= "):
                group, cur = line[2:].strip(), None
                self.groups.append(group)
                continue
            if line.startswith("hub "):
                if cur is None or cur.summary:
                    if group is None:
                        raise Bad(f"{n}: a command before the first group (= …)")
                    cur = Entry(group)
                    self.entries.append(cur)
                cur.forms.append(line)
                continue
            if cur is None:
                raise Bad(f"{n}: a line outside a command: {line[:60]}")
            if line.startswith(": "):
                if cur.summary:
                    raise Bad(f"{n}: a second summary line")
                cur.summary = line[2:].strip()
            elif line.startswith("  "):
                cur.details.append(line)
            elif line.startswith("-"):
                m = re.fullmatch(r"(\S+(?: \S+)*?)\s{2,}(\S.*)", line)
                name, _, arg = (m.group(1) if m else line).partition(" ")
                desc = m.group(2) if m else ""
                if not FLAG.fullmatch(name) or not desc:
                    raise Bad(f"{n}: a flag line is «--flag VALUE  what it does»: {line[:60]}")
                cur.flags.append((name, arg.strip(), desc.strip()))
            elif line.startswith("@ "):
                cur.who = line[2:].strip()
            elif line.startswith("$ "):
                cur.examples.append(line[2:].strip())
            elif line.startswith("! "):
                cur.errors.append(line[2:].strip())
            elif line.startswith("> "):
                cur.own = line[2:].strip()
            else:
                raise Bad(f"{n}: unknown line: {line[:60]}")
        keys = set()
        for e in self.entries:
            common = _key(e.forms[0])
            for w in map(_key, e.forms[1:]):
                n = 0
                while n < min(len(common), len(w)) and common[n] == w[n]:
                    n += 1
                common = common[:n]
            e.key = " ".join(common)
            if not e.key or not e.summary:
                raise Bad(f"{e.forms[0]}: no command name or no «: summary» line")
            if e.key in keys:
                raise Bad(f"{e.forms[0]}: the command {e.key} is described twice")
            keys.add(e.key)
        # a command whose own text lives in code cut out of this tree (the open flavor, core/cutout.py) is not this core's
        self.entries = [e for e in self.entries if present(e.own)]

    def tops(self):
        """The first words of the commands: what bin/hub knows (a module cannot take these names)."""
        return {e.key.split()[0] for e in self.entries}

    def of_top(self, top):
        return [e for e in self.entries if e.key.split()[0] == top]

    def flags(self, top, words=()):
        """(flags, flags with a value) of `hub <top> <words…>`: of its subcommand when the first word names one
        (`task note` has no --repo of `task run`), otherwise of all its forms."""
        sub = next(iter(words), "")
        exact = next((e for e in self.entries if e.key == f"{top} {sub}"), None) if WORD.fullmatch(sub) else None
        own = [exact] if exact else self.of_top(top)
        return set().union(*(e.flag_names() for e in own)), set().union(*(e.value_flags() for e in own))

    def unknown(self, top, words):
        """Words of `hub <top> <words…>` that look like a flag the command does not have, in any case. Only a command
        parsed by argparse (`% argparse`) takes `--name=value` and a unique start of a flag (`--sess` for --session);
        the others read exact words, so there these are refused too. A value after a flag that takes one is not
        checked; -h/--help stay (in free text they are words)."""
        names, valued = self.flags(top, words)
        loose = top in self.labels.get("argparse", "").split()
        bad, skip = [], False
        for w in words:
            if skip:
                skip = False
                continue
            m = TYPED.fullmatch(w)
            if not m or w in HELP_WORDS:
                continue
            name = m.group(1)
            if m.group(2) is not None and not loose:
                bad.append(w)
                continue
            if name not in names and name.startswith("--") and loose:
                starts = [n for n in names if n.startswith(name)]
                name = starts[0] if len(starts) == 1 and len(name) > 3 else name
            if name in names:
                skip = name in valued and m.group(2) is None
            else:
                bad.append(w)
        return bad

    def entry(self, top, words=()):
        """The entry `hub <top> <words…>` names: its subcommand's when the first word is one, else the bare
        `hub <top>`'s; None — neither."""
        sub = next(iter(words), "")
        by_key = {e.key: e for e in self.of_top(top)}
        if not WORD.fullmatch(sub):
            return by_key.get(top)
        bare = by_key.get(top)
        # no entry of its own: the entry whose form names it as a subcommand (`hub codex add | use <account> …`), the
        # bare one first (`hub codex [status|login|logout]` keeps login)
        named = [e for e in ([bare] if bare else []) + [e for e in self.of_top(top) if e is not bare]
                 if any(sub in _subwords(f, top) for f in e.forms)]
        return by_key.get(f"{top} {sub}") or (named[0] if named else bare)

    def subcommands(self, top):
        """The words after `hub <top>` its forms name: of entries of their own (`task run`) and of one entry's forms
        (`hub wait done`)."""
        words = [e.key.split()[1] for e in self.of_top(top) if " " in e.key]
        words += [f.split()[2] for e in self.of_top(top) for f in e.forms if len(f.split()) > 2
                  and WORD.fullmatch(f.split()[2])]
        return list(dict.fromkeys(words))

    def advice(self, top, words=(), token=None, unknown=False, more=True):
        """What follows a refusal of `hub <top> <words…>` (13.11: 173 refusals in a week named the rule, not the word,
        and sessions guessed again): the common mistakes (`!`) that name the wrong token, otherwise the closest known
        word; then the form of the subcommand the words name (all of the command's forms when they name none) and an
        example. unknown — the token is not one of the command's words (a flag, a subcommand): look for the closest."""
        e = self.entry(top, words)
        own = [e] if e else self.of_top(top)
        out = [f"  ! {x}" for x in mistakes(own, token)]
        close = []
        if token and unknown and not out:
            names = (sorted(set().union(*(x.flag_names() for x in own))) if token.startswith("-") else
                     self.subcommands(top))
            close = near(token, names)
            if close:
                out.append(f"  {self.labels.get('near', '')} {', '.join(close)}")
        forms = [form for x in own for form in x.forms]
        # the forms the words point at first: those of the word given (`module remove …` of the seven `module` forms),
        # or of the closest subcommands; the rest after them, six in all
        first = next((w for w in words if WORD.fullmatch(w)), "")
        aim = [f for f in forms if any(f.split()[2:3] == [w] for w in [first, *close] if w)]
        out += [f"  {form}" for form in (aim + [f for f in forms if f not in aim])][:6]
        example = next((x for e2 in own for x in e2.examples if not aim or any(x.split()[2:3] == f.split()[2:3]
                                                                                for f in aim)), None) or \
            next((x for e2 in own for x in e2.examples), None)
        if example:
            out.append(f"  {self.labels.get('example', '')} {example}")
        if more:
            out.append(f"  {self.labels.get('more', '{key}').format(key=e.key if e else top)}")
        return "\n".join(out)

    def misfit(self, top, words):
        """`hub <top> <words…>` that no branch took: the word the command does not know (a subcommand of a command
        that has only subcommands) and the closest ones, or None — the words do not fit the form."""
        subs = self.subcommands(top)
        first = next(iter(words), "")
        if subs and top not in {e.key for e in self.of_top(top)} and WORD.fullmatch(first) and first not in subs:
            return first
        return None

    def wrong_top(self, word):
        """The common mistakes (`!` of any command) that name `hub <word>` — a command sessions invent — and the closest
        commands."""
        lines = [f"  ! {x}" for x in mistakes(self.entries, f"hub {word}")]
        close = [] if lines else near(word, sorted(self.tops()), cutoff=0.8)   # list is not gist
        return lines + ([f"  {self.labels.get('near', '')} {', '.join('hub ' + c for c in close)}"] if close else [])

    def find(self, words):
        """The entry for the command words (`["task", "run"]`) and the entries under it: the longest prefix that names
        something, and that prefix. (None, [], "") — nothing."""
        words = [w for w in words if WORD.fullmatch(w)]
        for i in range(len(words), 0, -1):
            key = " ".join(words[:i])
            exact = next((e for e in self.entries if e.key == key), None)
            subs = [e for e in self.entries if e.key.startswith(key + " ")]
            if exact or subs:
                return exact, subs, key
        return None, [], ""

    # --- rendering ---
    def index(self, modules=(), hide=()):
        """hide — commands left out of the index (their own help still answers): an experiment off on this hub."""
        shown = [e for e in self.entries if e.key.split()[0] not in hide]
        width = max(len(e.key) for e in shown) + 2
        out = [self.labels.get("head", ""), *([self.labels["head2"]] if self.labels.get("head2") else []), ""]
        for g in self.groups:
            if any(e.group == g for e in shown):
                out.append(g + ":")
                out += [f"  {e.key:<{width}}{e.summary}" for e in shown if e.group == g]
                out.append("")
        if modules:
            out.append(self.labels.get("modules", ""))
            out += [f"  {cmd:<{width}}{self.labels.get('module', '{module}: {about}').format(module=mod, about=about)}"
                    for cmd, mod, about in modules]
        return "\n".join(out).rstrip() + "\n"

    def full(self, e, rule=""):
        out = list(e.forms) + ["  " + e.summary]
        for line in e.details:
            if line.strip() == RULE:
                rule = rule() if callable(rule) else rule
                out += ["  " + r for r in rule.split("\n")] if rule else []
            else:
                out.append(line)
        if e.own:
            out.append(own_text(e.own))
        if e.flags:
            width = max(len(f + (" " + a if a else "")) for f, a, _ in e.flags) + 2
            out.append(self.labels.get("flags", ""))
            out += [f"  {(f + (' ' + a if a else '')):<{width}}{d}" for f, a, d in e.flags]
        if e.who:
            out.append(f"{self.labels.get('who', '')} {e.who}")
        if e.examples:
            out.append(self.labels.get("examples", ""))
            out += ["  " + x for x in e.examples]
        if e.errors:
            out.append(self.labels.get("errors", ""))
            out += ["  " + x for x in e.errors]
        return "\n".join(out) + "\n"

    def brief(self, e):
        return "\n".join(list(e.forms) + ["  " + e.summary]) + "\n"

    def render(self, words, rule=""):
        """The help of `hub <words…>` or None (no such command)."""
        exact, subs, key = self.find(words)
        if not exact and not subs:
            return None
        named = self.entry(words[0], words[1:]) if exact and len(words) > 1 and exact.key == words[0] else None
        if named is not None and named is not exact:   # `hub codex use`: the entry whose form names `use`
            return self.full(named, rule)
        parts = [self.full(exact, rule)] if exact else []
        parts += [self.brief(e) for e in subs]
        if subs:
            parts.append(self.labels.get("more", "{key}").format(key=key + " <…>"))
        return "\n".join(parts).rstrip() + "\n"


def near(word, names, n=3, cutoff=0.7):
    """Known words close to a mistyped one: the same parts in another order (--notes-append for --append-notes), then
    by spelling (-o is no closer to --out than anything: a `!` line says that one)."""
    def parts(w):
        return sorted(w.strip("-").split("-"))
    same = [x for x in names if parts(x) == parts(word) and x != word]
    close = difflib.get_close_matches(word, names, n=n, cutoff=cutoff)
    return list(dict.fromkeys(same + close))[:n]


def mistakes(entries, token):
    """The `!` lines of the entries that name the token as a word of its own (`-o` is not in `--out`)."""
    if not token:
        return []
    pat = re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])")
    return list(dict.fromkeys(x for e in entries for x in e.errors if pat.search(x)))


def _subwords(form, top):
    """The subcommand words a form names right after `hub <top>`: the first word of each `|` alternative, brackets
    off (`hub codex [status|login|logout]` -> status, login, logout)."""
    rest = form.split(None, 2)[2] if len(form.split(None, 2)) > 2 and form.split()[1] == top else ""
    return {seg.strip().strip("[]()").split(" ")[0].strip("[]()") for seg in rest.split("|") if seg.strip()}


def present(ref):
    """The code a `> module:NAME` reference names is in this tree: only the built-in code the open flavor cuts out may be
    missing (core/cutout.py); no reference, i18n, the rest — yes."""
    kind = (ref or "").partition(":")[0]
    from core import cutout
    rel = kind.replace(".", "/") + ".py"
    return next((cutout.present(name) for name, path in cutout.FILES.items() if path == rel), True)


def own_text(ref):
    """The help a command keeps with its own parsing (`module:NAME` or `i18n:key`), so it is written once."""
    kind, _, name = ref.partition(":")
    if kind == "i18n":
        from core import i18n
        return "  " + i18n.t(name).replace(" | ", "\n  ").replace("; ", "\n  ")
    import importlib
    text = getattr(importlib.import_module(kind), name).rstrip()
    if CATALOG_KEY.fullmatch(text):   # the name holds its line's catalog key (core.codexcli:USAGE): the line, as i18n:
        return own_text(f"i18n:{text}")
    return text


CATALOG_KEY = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+")


_LOADED = {}


def load(path=HELP):
    """The parsed help, once per process and file version (bin/hub asks for it several times in one call)."""
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        stamp = None
    if (path, stamp) not in _LOADED:
        with open(path, encoding="utf-8") as f:
            _LOADED[(path, stamp)] = Help(f.read())
    return _LOADED[(path, stamp)]
