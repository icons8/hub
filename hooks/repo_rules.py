"""Codex (experimental): a repo's Claude Code permission rules (`permissions.deny|ask|allow` of
.claude/settings.json and .claude/settings.local.json) for a hub's Codex session. With Claude, Claude Code applies them
itself (--setting-sources project,local); Codex knows nothing of them, so the core's Codex hook (hooks/deny_hint.py)
applies them on every call, by the semantics Claude Code 2.1.309 shows (probe 2026-11-05: the real claude on a fake
Anthropic API with a fake shell; tests/test_codex_repo_rules.py holds its verdicts):

- the files: settings.json of the launch folder only (not of a parent); settings.local.json of the launch folder and of
  the canonical repo root (a subfolder, a git worktree: the main checkout's), as its storage resolves localSettings;
- Bash(rule): the command is split into simple commands (`&&`, `||`, `;`, `|`, `&`, a new line, `$(…)`, backticks,
  `(…)`, `{ …; }`, if/for/while bodies), each without its env assignments, redirections, wrappers (timeout, nice,
  nohup, env, time, watch, command, exec, xargs) and the program's path (/usr/bin/git -> git); white space collapsed.
  `prefix:*` — the prefix as whole words; otherwise a pattern where only `*` is a wildcard (also over spaces; a
  backslash is itself), anchored on both ends, and a trailing ` *` may match nothing (`ls *` takes `ls`).
  `bash -c '…'`, `eval`, `git -C . push` are not looked into, as with Claude;
- Read(path) deny also holds reading commands (cat, head, grep, sed, cp's sources, `< file` …), Edit(path) and
  Write(path) deny writing ones (`>`, `>>`, tee, sed -i, touch, rm, mv, cp's target …); a path's ask rule holds only
  the file tools (apply_patch, view_image here), not the shell; a word with a glob or `$` is not a path, as with
  Claude. Paths are gitignore patterns: `//x` absolute (`//c/x` — C:\\x), `~/x` the home, `/x` and `./x` and `x/y`
  from the project root, a name without `/` at any depth; `**` any folders; a match of a folder holds its contents;
- precedence deny > ask > allow; one denied simple command denies the whole command.

Stricter than Claude Code (the safe side): a pattern is also tried on the command without quotes (`git push origin
'main'` with `git push * main`), a rule names a wrapper too (`timeout:*`), a path is also tried by its real path and
after a `cd` earlier in the line, a line continuation is joined. Not held: WebFetch/WebSearch (Codex has no such tool;
its web search runs at OpenAI and no hook sees it). MCP rules hold by the tool's name; in 0.162.1 Codex hides MCP tools
behind tool_search, which the probe's fake API cannot drive: not checked live. The hook loads it on every call:
stdlib at the top; the core's own path helpers (core.mcplogin._git_root, core.minihub._rule_path) when it needs them.
"""
import fnmatch
import functools
import itertools
import json
import os
import re

KINDS = ("deny", "ask", "allow")
FILE_TOOLS = {"Read": "read", "Edit": "edit", "Write": "edit", "NotebookEdit": "edit"}
# programs whose file arguments are read (Claude Code checks them against Read rules) and the ones that write them
READERS = {"cat", "head", "tail", "wc", "ls", "diff", "cmp", "nl", "od", "xxd", "hexdump", "strings", "file", "stat",
           "md5sum", "sha1sum", "sha256sum", "base64", "sort", "uniq", "cut", "tac", "rev", "column", "jq", "yq",
           "grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "gawk", "cp", "bat", "find",
           # viewers and editors read their files too (release review 2026-11-09: `less .env` went past a Read deny)
           "less", "more", "most", "view", "vi", "vim", "nvim", "nano", "emacs", "iconv", "zcat", "zless", "bzcat",
           "xzcat"}
WRITERS = {"tee", "touch", "rm", "mv", "rmdir", "mkdir", "truncate", "shred", "ln", "unlink"}
# the first word that is not a file: the pattern of grep/rg, the script of sed/awk (unless given by -e/-f)
SCRIPT_FIRST = {"grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "gawk", "jq", "yq"}
WRAPPERS = {"timeout", "nice", "nohup", "env", "time", "watch", "command", "exec", "xargs", "stdbuf", "ionice"}
# options of a wrapper that take a value
TAKES = {"timeout": {"-s", "--signal", "-k", "--kill-after"}, "nice": {"-n", "--adjustment"},
         "watch": {"-n", "--interval", "-d"}, "xargs": {"-n", "-I", "-P", "-L", "-d", "-s", "-E", "-a"},
         "stdbuf": {"-i", "-o", "-e"}, "ionice": {"-c", "-n", "-p"}, "env": {"-u", "-C", "-S"}}
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}", "case", "esac"}
ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
REDIR_OP = re.compile(r"&>>|&>|>>|>\||>&|>|<<<|<&|<>|<")
REDIRECT = re.compile(r"(\d*)(&>>|&>|>>|>\||>&|>|<<<|<&|<>|<)")
HEREDOC = re.compile(r"<<-?[ \t]*((?:'[^'\n]*'|\"[^\"\n]*\"|[^\s;&|<>()'\"])+)")
DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/tty"}
GLOB = re.compile(r"[*?\[$`]")
# the file lines of Codex's patch (a \r\n patch as Codex reads it): the path; and with the line's kind
# (hooks/spec_guard: only Add and Update may touch the spec). Codex trims the spaces before a file line and applies an
# indented one too (release review 2026-11-09): read it so — a context line that only looks like one is checked as well
PATCH_PATH = re.compile(r"^[ \t]*\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)[ \t\r]*$", re.M)
PATCH_FILE = re.compile(r"^[ \t]*\*\*\* (Add File|Update File|Delete File|Move to): (.+?)[ \t\r]*$", re.M)
RULE = re.compile(r"\A([A-Za-z0-9_.\-*]+)(?:\((.*)\))?\Z", re.S)
ANSI = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b", "e": "\x1b", "f": "\f",
        "v": "\v"}


ESCAPE = re.compile(r"x([0-9A-Fa-f]{1,2})|([0-7]{1,3})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|c(.)")


def _escape(m):
    hexa, octal, u4, u8, ctrl = m.groups()
    if ctrl is not None:
        return chr(ord(ctrl) & 0x1f)
    code = int(hexa or u4 or u8, 16) if (hexa or u4 or u8) else int(octal, 8)
    return chr(code) if code < 0x110000 else ""


class Raw(str):
    """A word that is not a literal (it holds `$(…)` or a backtick): its text as written; no path, no program name."""


def parse(rule):
    """'Bash(git push:*)' -> ('Bash', 'git push:*'); 'Read' -> ('Read', None); not a rule -> None. `\\(` and `\\)` in the
    content are parentheses."""
    if not isinstance(rule, str):
        return None
    m = RULE.match(rule.strip())
    if not m:
        return None
    content = m.group(2)
    if content is not None:
        content = content.replace("\\(", "(").replace("\\)", ")")
        if content in ("", "*"):
            content = None
    return m.group(1), content


# --- the files

SOURCES = ("project", "local")   # the hub's --setting-sources by default


def files(cwd, sources=SOURCES):
    """The settings files a Claude session launched in cwd reads with --setting-sources `sources`, in order: project —
    <cwd>/.claude/settings.json, local — settings.local.json of cwd and of the repo's main checkout root."""
    if not cwd:
        return []
    from core.mcplogin import _git_root
    cwd = os.path.abspath(cwd)
    # never a home folder's: that is the user's own ~/.claude/settings.json, which a hub session does not get (as
    # hosts/codex_set.repo_hooks)
    homes = {os.path.normcase(os.path.realpath(h)) for h in (os.path.expanduser("~"), os.environ.get("USERPROFILE"))
             if h}
    out = [os.path.join(cwd, ".claude", "settings.json")] if "project" in sources else []
    if "local" in sources:
        out.append(os.path.join(cwd, ".claude", "settings.local.json"))
        root = _git_root(cwd)
        if os.path.realpath(root) != os.path.realpath(cwd):
            out.append(os.path.join(root, ".claude", "settings.local.json"))
    return [f for f in out if os.path.normcase(os.path.realpath(os.path.dirname(os.path.dirname(f)))) not in homes]


def load(paths):
    """{kind: [(tool, content, rule, file)]} from the settings files. A missing or broken file is skipped, as Claude
    Code skips it (it warns and goes on without it)."""
    out = {k: [] for k in KINDS}
    for path in paths or ():
        try:
            with open(path, encoding="utf-8") as f:
                got = json.load(f)
        except (OSError, ValueError):
            continue
        perms = got.get("permissions") if isinstance(got, dict) else None
        if not isinstance(perms, dict):
            continue
        for kind in KINDS:
            for rule in perms.get(kind) if isinstance(perms.get(kind), list) else ():
                p = parse(rule)
                if p:
                    out[kind].append((p[0], p[1], rule, path))
    return out


# --- the shell

OPEN, CLOSE = ("(", None), (")", None)   # in the list of _split: a subshell or a substitution begins, ends


def _split(command):
    """The simple commands of a shell line: [(raw text, [words])]; words without quotes (Raw — a word with $(…) or a
    backtick inside, not a literal). Heredoc bodies and comments are skipped; command substitutions and subshells are
    split too, between the OPEN and CLOSE entries (words None: what a `cd` inside leads to stays inside)."""
    out = []
    _scan(command, out)
    return out


def _nested(s, out):
    """The commands of a $(…) or `…` body, between the OPEN and CLOSE entries."""
    out.append(OPEN)
    _scan(s, out)
    out.append(CLOSE)


def _scan(s, out):
    i, n = 0, len(s)
    raw, words, heredocs = [], [], []
    word, lit, wraw = None, True, []

    def end_word():
        nonlocal word, lit
        if word is not None:
            words.append(word if lit else Raw("".join(wraw)))
        word, lit = None, True
        wraw.clear()

    def add(text, value):
        nonlocal word
        word = (word or "") + value
        wraw.append(text)
        raw.append(text)

    def end_cmd():
        end_word()
        text = " ".join("".join(raw).split())
        if text or words:
            out.append((text, list(words)))
        raw.clear()
        words.clear()

    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            if s[i + 1] == "\n":   # a line continuation
                i += 2
                continue
            add(s[i:i + 2], s[i + 1])
            i += 2
            continue
        if c == "$" and s.startswith("$'", i):   # ANSI-C quoting: $'git'
            j, part = i + 2, []
            while j < n and s[j] != "'":
                if s[j] == "\\" and j + 1 < n:
                    m = ESCAPE.match(s, j + 1)
                    if m:   # \xHH, \NNN, \uHHHH, \UHHHHHHHH, \cX
                        part.append(_escape(m))
                        j = m.end()
                        continue
                    part.append(ANSI.get(s[j + 1], s[j + 1]))
                    j += 2
                    continue
                part.append(s[j])
                j += 1
            add(s[i:j + 1], "".join(part))
            i = j + 1
            continue
        if c == "'":
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            add(s[i:j + 1], s[i + 1:j])
            i = j + 1
            continue
        if c == '"' or (c == "$" and s.startswith('$"', i)):
            start = i
            j, part = i + (2 if c == "$" else 1), []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n:
                    if s[j + 1] != "\n":   # a line continuation: bash drops both (review 2026-11-09)
                        part.append(s[j + 1])
                    j += 2
                    continue
                if s.startswith("$(", j) or s[j] == "`":
                    k = _sub_end(s, j)
                    _nested(s[j + 2:k - 1] if s[j] == "$" else s[j + 1:k - 1], out)
                    lit = False
                    part.append(s[j:k])
                    j = k
                    continue
                part.append(s[j])
                j += 1
            add(s[start:j + 1], "".join(part))
            i = j + 1
            continue
        if s.startswith("$(", i) and not s.startswith("$((", i) or c == "`":
            k = _sub_end(s, i)
            _nested(s[i + 2:k - 1] if c == "$" else s[i + 1:k - 1], out)
            add(s[i:k], s[i:k])
            lit = False
            i = k
            continue
        if c == "#" and word is None:
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if s.startswith("<<", i) and not s.startswith("<<<", i):
            m = HEREDOC.match(s, i)
            if m:
                end_word()
                # <<\\END, <<E"N"D: the tag is END; a quoted tag — a literal body, otherwise bash expands it
                heredocs.append((re.sub(r"[\\\\'\"]", "", m.group(1)), not re.search(r"[\\\\'\"]", m.group(1))))
                raw.append(m.group(0))
                i = m.end()
                continue
        if c == "\n":
            end_cmd()
            i += 1
            for tag, expands in heredocs:   # the body up to its end line
                m = re.compile(r"^[ \t]*" + re.escape(tag) + r"[ \t]*$", re.M).search(s, i)
                if expands:   # an unquoted body: its $(…) and `…` run as commands
                    _substitutions(s[i:m.start() if m else n], out)
                i = n if not m else m.end()
            heredocs = []
            continue
        if c in "<>" or (c == "&" and s.startswith("&>", i)):
            m = REDIR_OP.match(s, i)
            prefix = word if word is not None and lit and word.isdigit() else None
            if prefix is None:
                end_word()
            else:
                word, wraw[:] = None, []
            words.append((prefix or "") + m.group(0))
            raw.append(m.group(0))
            i = m.end()
            continue
        if c in ";|&()":
            end_cmd()
            if c in "()":
                out.append(OPEN if c == "(" else CLOSE)
            i += 2 if s[i:i + 2] in ("&&", "||", ";;", "|&") else 1
            continue
        if c in " \t":
            end_word()
            raw.append(" ")
            i += 1
            continue
        add(c, c)
        i += 1
    end_cmd()


def _substitutions(body, out):
    """The commands of the $(…) and `…` in an unquoted heredoc body (the rest of it is text)."""
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body.startswith("$(", i) and not body.startswith("$((", i) or body[i] == "`":
            k = _sub_end(body, i)
            _nested(body[i + 2:k - 1] if body[i] == "$" else body[i + 1:k - 1], out)
            i = k
            continue
        i += 1


def _sub_end(s, i):
    """The index after the end of $(…) or `…` that starts at i."""
    if s[i] == "`":
        j = i + 1
        while j < len(s) and s[j] != "`":
            j += 2 if s[j] == "\\" else 1
        return min(j + 1, len(s))
    depth, j, q = 0, i + 1, None
    while j < len(s):
        c = s[j]
        if q:
            if c == q:
                q = None
            elif c == "\\" and q == '"':
                j += 1
        elif c in "'\"":
            q = c
        elif c == "\\":
            j += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return len(s)


def _literal(w):
    return w is not None and not isinstance(w, Raw)


def _braces(word, limit=64):
    """The words bash makes of a word with brace expansions (`{rm,-rf,x}`, `r{m,}`, `{~/.ssh/k,a}`, nested — innermost
    first; a quoted brace is expanded too: the safe side); at most `limit` of them."""
    m = BRACE.search(word)
    if not m:
        return [word] if word else []
    out = []
    for part in m.group(0)[1:-1].split(","):
        out += _braces(word[:m.start()] + part + word[m.end():], limit)
        if len(out) >= limit:
            break
    return out[:limit]


def _simple(words):
    """The forms of one simple command to match rules against: [[words]] — as written, then without env
    assignments, leading keywords, wrappers and the program's path; redirections are out of all of them. And its
    redirection targets: [(op, path)]."""
    redirs, plain = [], []
    words = [x for w in words for x in (_braces(w) if _literal(w) else [w])]
    k = 0
    while k < len(words):
        w = words[k]
        m = REDIRECT.fullmatch(w) if _literal(w) else None
        if m:
            target = words[k + 1] if k + 1 < len(words) else None
            k += 2
            if m.group(2) not in ("<&", "<<<") and target is not None and not (target.isdigit() or target == "-"):
                redirs.append(("<" if m.group(2) == "<" else ">", target))
            continue
        plain.append(w)
        k += 1
    forms = []
    rest = plain
    if rest and rest[0] == "function":   # `function f { …; }`, `function f() …`: the body is the command (review)
        rest = rest[2:]
        if rest and rest[0] == "()":
            rest = rest[1:]
    if rest and rest[0] == "coproc":   # `coproc cmd`, `coproc name { …; }`
        rest = rest[1:]
        if len(rest) > 1 and rest[1] in ("{", "("):
            rest = rest[1:]
    while rest and rest[0] in KEYWORDS:
        rest = rest[1:]
    if rest and rest[0] == "for":   # `for x in a b` is no command; its body is the next part
        rest = []
    while rest and ASSIGN.match(rest[0]):   # X=1, also X="$(date)": the program comes after
        rest = rest[1:]
    seen = set()
    while rest:
        key = tuple(rest)
        if key in seen:
            break
        seen.add(key)
        forms.append(rest)
        first = rest[0]
        if not _literal(first):
            break
        base = first.rsplit("/", 1)[-1] if "/" in first else first
        if base != first:
            rest = [base] + rest[1:]
            continue
        if base in WRAPPERS:
            rest = _unwrap(base, rest[1:])
            continue
        break
    return forms, redirs


def _unwrap(name, args):
    """The command a wrapper runs: its options and their values dropped (timeout 5 x, nice -n 10 x, env A=1 x)."""
    i = 0
    takes = TAKES.get(name, set())
    while i < len(args) and _literal(args[i]) and args[i].startswith("-") and args[i] != "-":
        i += 2 if args[i] in takes else 1
    if name == "timeout" and i < len(args):   # the duration
        i += 1
    if name == "env":
        while i < len(args) and ASSIGN.match(args[i]):
            i += 1
    return args[i:]


@functools.lru_cache(maxsize=512)
def _pattern(content):
    """A Bash rule as a regular expression over a simple command's text."""
    if content.endswith(":*"):
        prefix = " ".join(content[:-2].split())
        return re.compile(re.escape(prefix) + r"(?:\s.*)?\Z", re.S)
    norm = " ".join(content.split())
    trailing = norm.endswith(" *")
    if trailing:
        norm = norm[:-2]
    out = [".*" if ch == "*" else re.escape(ch) for ch in norm]
    return re.compile("".join(out) + (r"(?: .*)?" if trailing else "") + r"\Z", re.S)


def _bash_match(content, form, text=None):
    """A Bash rule against one form of a simple command: its words joined, and the written text (the first form)."""
    if content is None:
        return True
    pat = _pattern(content)
    joined = " ".join(form)
    return any(t and pat.match(t) for t in {text, joined})


# --- paths

def _segments_match(pat, path):
    """gitignore-like: `**` any number of folders (also none), other segments by fnmatch; a match of a leading part
    of the path holds the rest (a folder's contents)."""
    if not pat:
        return True
    if pat[0] == "**":
        return any(_segments_match(pat[1:], path[k:]) for k in range(len(path) + 1))
    if not path:
        return False
    return fnmatch.fnmatchcase(path[0], pat[0]) and _segments_match(pat[1:], path[1:])


@functools.lru_cache(maxsize=4096)
def _forms(path):
    """A path in Claude Code's form (/x/y; Windows C:\\x -> /c/x), plain and real (a link to .env is .env)."""
    from core.minihub import _rule_path
    return frozenset((_rule_path(path), _rule_path(path, real=True)))


# the default file systems of macOS (APFS) and Windows (NTFS) ignore case: .ENV is .env there (release review 2026-11-09);
# a case-sensitive volume of such a machine is held the stricter way
FOLD = None   # None — the platform's word (platforms CASE_BLIND), read at the first match: stdlib at the top here


@functools.lru_cache(maxsize=1)
def _case_blind():
    from platforms import current
    return bool(current.CASE_BLIND)   # a hook runs with the platforms of its own tree: no old adapter


def path_match(pattern, path, root, home=None):
    """A Read/Edit rule's path pattern against an absolute path. root — the project folder."""
    home = home or os.path.expanduser("~")
    blind = _case_blind() if FOLD is None else FOLD
    fold = str.casefold if blind else str   # after _forms: realpath answers in the disk's own case (review)
    p = fold(pattern.replace("\\", "/"))
    if p.startswith("//"):
        bases, p, anchored = {""}, p[2:], True
    elif p == "~" or p.startswith("~/"):
        bases, p, anchored = set(map(fold, _forms(home))), p[2:], True
    elif p.startswith("/"):
        bases, p, anchored = set(map(fold, _forms(root))), p[1:], True
    else:
        if p.startswith("./"):
            p, anchored = p[2:], True
        else:
            anchored = "/" in p.rstrip("/")
        bases = set(map(fold, _forms(root)))
    p = p.rstrip("/")
    pat = [x for x in p.split("/") if x] if p else ["**"]
    if not anchored:
        pat = ["**"] + pat
    for full in map(fold, _forms(path)):
        for base in bases:
            base = base.rstrip("/")
            if full != base and not full.startswith(base + "/"):
                continue
            rest = full[len(base):].strip("/")
            if _segments_match(pat, rest.split("/") if rest else []):
                return True
    return False


def _abs(word, cwd, home):
    if word == "~" or word.startswith("~/"):
        word = os.path.join(home, word[2:])
    return os.path.normpath(os.path.join(cwd, word))


BRACE = re.compile(r"\{[^{}\s]*,[^{}\s]*\}")   # an innermost brace expansion in a word
SHORT_F = re.compile(r"-[A-Za-z]*f")   # a cluster of short options ending in -f (sed -nf FILE, grep -rf FILE)
FIND_FILES = {"-newer", "-anewer", "-cnewer", "-samefile"}   # find's options whose value is a file


def _file_args(prog, args):
    """(read, written) file words of a reading or writing program."""
    if prog == "find":   # its start folders and the files its tests name (-newer x), not its other words
        out, start = [], True
        for k, a in enumerate(args):
            if start and _literal(a) and (a.startswith(("-", "(", "!")) or a == ")"):
                start = False
            if start:
                out.append(a)
            elif _literal(a) and a in FIND_FILES and k + 1 < len(args):
                out.append(args[k + 1])
        return out, []
    files, script, opts_done, i = [], prog in SCRIPT_FIRST, False, 0
    in_place, target = False, None
    while i < len(args):
        a = args[i]
        if not _literal(a):
            pass
        elif not opts_done and a == "--":
            opts_done = True
        elif not opts_done and a.startswith("-") and a != "-":
            if prog == "sed" and (a.startswith("--in-place") or not a.startswith("--") and "i" in a[1:]):
                in_place = True
            if prog in SCRIPT_FIRST and (a in ("-f", "--file", "--from-file") or SHORT_F.fullmatch(a)) \
                    and i + 1 < len(args):   # also -nf, -Ef: the cluster ends with the option taking the file
                script = False   # the script is read from a file: that file is a read too
                files.append(args[i + 1])
                i += 1
            elif prog in SCRIPT_FIRST and a.startswith(("--file=", "--from-file=")):
                script = False
                files.append(a.split("=", 1)[1])
            elif prog in SCRIPT_FIRST and a in ("-e", "--regexp", "--expression"):
                script = False
                i += 1
            elif prog in SCRIPT_FIRST and a.startswith(("--regexp=", "--expression=")):   # the pattern attached
                script = False
            elif prog in SCRIPT_FIRST and not a.startswith("--") and "f" in a[1:]:   # -fFILE, -nfFILE: attached
                script = False
                files.append(a[a.index("f", 1) + 1:])
            elif prog in SCRIPT_FIRST and not a.startswith("--") and "e" in a[1:]:   # -ex, -rex; -re: the next word
                script = False
                if a.endswith("e"):
                    i += 1
            elif prog == "cp" and a in ("-t", "--target-directory") and i + 1 < len(args):
                target = args[i + 1]
                i += 1
            elif prog == "cp" and not a.startswith("--") and "t" in a[1:]:   # -rt /tmp, -vt /tmp, -t/tmp
                rest = a[a.index("t", 1) + 1:]
                if rest:
                    target = rest
                elif i + 1 < len(args):
                    target = args[i + 1]
                    i += 1
            elif prog == "cp" and a.startswith("--target-directory="):
                target = a.split("=", 1)[1]
            elif prog in ("head", "tail") and a in ("-n", "-c"):
                i += 1
        elif script:
            script = False
        else:
            files.append(a)
        i += 1
    if prog in WRITERS or in_place:
        return [], files
    if prog == "cp" and target is not None:   # cp -t <folder> <sources…> (review 2026-11-09)
        return files, [target]
    if prog == "cp" and files:
        return files[:-1], files[-1:]
    return files, []


def _paths(forms, redirs, cwds, home):
    """(read paths, written paths, whether some path is unknown: a glob, $… or a substitution) of a simple command,
    each relative word resolved against every folder in cwds (None in cwds — a folder that is not known)."""
    read, written, unknown = [], [], [None in cwds]

    def put(dst, word):
        if word in DEVICES:
            return
        if _literal(word) and not GLOB.search(word):
            dst.extend(_abs(word, cwd, home) for cwd in cwds if cwd is not None)
            return
        unknown[0] = True
        # a glob: its folders before the first wildcard are a path too (modules/secrets/*.py is in modules/secrets)
        folders = _literal(word) and "$" not in word and "`" not in word and word.replace("\\", "/").split("/")
        if folders:
            lead = list(itertools.takewhile(lambda x: not GLOB.search(x), folders[:-1]))
            if lead and any(lead):
                dst.extend(_abs("/".join(lead) or "/", cwd, home) for cwd in cwds if cwd is not None)
    for op, target in redirs:
        put(read if op == "<" else written, target)
    form = forms[-1] if forms else None
    if form and _literal(form[0]) and form[0] == "git" and len(form) > 2 and form[1] in ("diff", "grep"):
        # git diff <revs and paths>, git grep <pattern> [paths] [-- paths]: Claude Code reads them as paths
        args = [a for a in form[2:] if not (_literal(a) and a.startswith("-") and a != "--")]
        if form[1] == "grep":
            args = form[form.index("--") + 1:] if "--" in form else args[1:]
        for a in args:
            if a != "--":
                put(read, a)
    elif form and _literal(form[0]):
        prog = form[0]
        if prog in READERS or prog in WRITERS:
            r, w = _file_args(prog, form[1:])
            for a in r:
                put(read, a)
            for a in w:
                put(written, a)
    return read, written, unknown[0]


# --- the decision

def _first(rules, kinds, test):
    for kind in kinds:
        for tool, content, rule, path in rules.get(kind) or ():
            if test(tool, content):
                return kind, rule, path
    return None


def _parts(command, root, cwd, home, follow=False):
    """[(text, forms, read, written)] of the simple commands; a `cd <folder>` adds a folder later paths are tried in;
    a cd the scan cannot follow, or a path it cannot know, is a None in written (no allow for such a line). follow — a
    relative path is tried in the folder the last `cd` led to only, not in the earlier ones too (the stricter way would
    take `cd /tmp/x && echo > f` for a write into the launch folder). A `cd` in a subshell or a substitution ends
    with it."""
    cwds, parts, outer = [cwd], [], []
    for text, words in _split(command):
        if words is None:   # a subshell: its `cd` does not reach the commands after it (a stray `)` closes nothing)
            if text == "(":
                outer.append(cwds)
            elif outer:
                cwds = outer.pop()
            continue
        forms, redirs = _simple(words)
        if not forms and not redirs:
            continue
        read, written, unknown = _paths(forms, redirs, cwds, home)
        parts.append((text, forms, read, written + ([None] if unknown else [])))
        last = forms[-1] if forms else []
        if last and last[0] in ("cd", "pushd", "popd"):
            known = len(last) == 2 and _literal(last[1]) and not GLOB.search(last[1]) and not last[1].startswith("-")
            base = next((c for c in reversed(cwds) if c), cwd)
            # after a cd the scan could not follow there is no base for a relative path, an absolute one needs none
            blind = follow and None in cwds and not (os.path.isabs(last[1]) or last[1] in ("~",) or
                                                     last[1].startswith("~/"))
            there = _abs(last[1], base, home) if known and not blind else None
            cwds = [there] if follow else cwds + [there]
    return parts


def _inside(path, root):
    return any(f == r or f.startswith(r.rstrip("/") + "/") for f in _forms(path) for r in _forms(root))


def bash(command, rules, root, cwd=None, home=None, follow=False):
    """(kind, rule, file) of the strongest rule a shell command meets — deny, then ask; allow only if every simple
    command meets an allow rule and the line writes nothing and reads only inside the project (Claude Code would ask
    about the rest). None — no rule."""
    home = home or os.path.expanduser("~")
    parts = _parts(command, root, cwd or root, home, follow)

    def meets(kinds, part):
        text, forms, read, written = part
        files = kinds == ("deny",)   # Claude Code holds a path's ask rule on its file tools only, not on the shell
        return _first(rules, kinds, lambda tool, content: (
            tool == "Bash" and any(_bash_match(content, f, text if k == 0 else None) for k, f in enumerate(forms))
            or files and FILE_TOOLS.get(tool) == "read" and any(content is None or path_match(content, p, root, home)
                                                                 for p in read)
            or files and FILE_TOOLS.get(tool) == "edit" and any(p is not None and (content is None or
                                                                                   path_match(content, p, root, home))
                                                                 for p in written)))
    for kinds in (("deny",), ("ask",)):
        for part in parts:
            hit = meets(kinds, part)
            if hit:
                return hit
    if not parts or any(written or not all(_inside(p, root) for p in read) for _, _, read, written in parts):
        return None
    hits = [_first(rules, ("allow",), lambda tool, content, f=forms, t=text: tool == "Bash" and any(
        _bash_match(content, x, t if k == 0 else None) for k, x in enumerate(f)))
            for text, forms, _, _ in parts if not _cd_inside(forms, root, cwd or root, home)]
    return hits[0] if hits and all(hits) else None


def _cd_inside(forms, root, cwd, home):
    """`cd <folder inside the project>`: Claude Code needs no rule for it."""
    last = forms[-1] if forms else []
    return (len(last) == 2 and last[0] == "cd" and _literal(last[1]) and not GLOB.search(last[1])
            and _inside(_abs(last[1], cwd, home), root))


def paths(kind, targets, rules, root, home=None):
    """(kind, rule, file) of the strongest rule for file tool calls on targets (kind: read | edit); allow only if all
    targets are allowed."""
    home = home or os.path.expanduser("~")
    targets = [os.path.normpath(os.path.join(root, t)) for t in targets if isinstance(t, str) and t]
    if not targets:
        return None

    def test(t):
        return lambda tool, content: FILE_TOOLS.get(tool) == kind and (content is None or
                                                                        path_match(content, t, root, home))
    for kinds in (("deny",), ("ask",)):
        for t in targets:
            hit = _first(rules, kinds, test(t))
            if hit:
                return hit
    hits = [_first(rules, ("allow",), test(t)) for t in targets]
    return hits[0] if all(hits) else None


def _named(rules, kinds, name):
    """A rule naming a tool that is no Bash and no file tool (an MCP tool: mcp__server, mcp__server__tool, a glob)."""
    return _first(rules, kinds, lambda tool, content: content is None and tool not in FILE_TOOLS and tool != "Bash" and (
        tool == name or name.startswith(tool + "__") or fnmatch.fnmatchcase(name, tool)))


def call(tool, tool_input, rules, root, cwd=None, home=None):
    """(kind, rule, file) for one Codex tool call: Bash — the command; apply_patch — its paths as an edit; view_image —
    its path as a read; another tool — by its name."""
    cwd = cwd or root
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if tool == "Bash" and isinstance(command, str):
        return bash(command, rules, root, cwd, home)
    if tool == "apply_patch" and isinstance(command, str):
        return paths("edit", [os.path.join(cwd, p.strip()) for p in PATCH_PATH.findall(command)], rules, root, home)
    if tool == "view_image" and isinstance(tool_input.get("path"), str):
        return paths("read", [os.path.join(cwd, tool_input["path"])], rules, root, home)
    hit = _named(rules, ("deny", "ask"), tool)
    return hit or _named(rules, ("allow",), tool)


DENY = ("Denied by the repo rule `{rule}` ({file}): this is not allowed in this repo — Claude Code would refuse it "
        "the same way. Do not get around the ban with another command; if you need exactly this, tell the owner or "
        "whoever gave you the task why")
ASK = ("The repo rule `{rule}` ({file}) requires the owner's \"yes\" on every such call. The Codex hub cannot ask him "
       "with a card from a hook, so the call was not run. If you need it, ask the owner with a form what to do; do "
       "not work around it yourself")


def reason(tool, tool_input, settings, root, cwd=None, home=None):
    """The refusal text for a Codex call by the repo's rules (deny and ask both refuse), or None."""
    if not settings or not root:
        return None
    hit = call(tool, tool_input, load(settings), root, cwd, home)
    if not hit or hit[0] == "allow":
        return None
    kind, rule, path = hit
    rel = path
    for p, r in ((os.path.abspath(path), os.path.abspath(root)), (os.path.realpath(path), os.path.realpath(root))):
        if p.startswith(r.rstrip(os.sep) + os.sep):
            rel = os.path.relpath(p, r)
            break
    return (DENY if kind == "deny" else ASK).format(rule=rule, file=rel.replace("\\", "/"))
