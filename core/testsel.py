"""Which test modules a diff touches (`tools/test.py --changed [base]`): a static graph of "this file depends on that one"
over the repo, reversed from the changed files. Nothing is run here; the answer is a list of test modules with a reason
for each, or "run everything" with a reason.

Edges (A depends on B):
- import: `import` between Python files (core/, modules/, hosts/, hooks/, platforms/, tests/, tools/…; scripts without
  .py — `bin/*` — by their shebang) and ES-module imports between page scripts. Always followed, transitively;
- reference: a path or name a file mentions without importing — a path built from constants
  (`os.path.join(ROOT, "bin", "hub")`, `ROOT / "i18n"`, an f-string), a string that is wholly a path ("static/js/x.js",
  read from the root, from the file's own folder and the ones above it, or as a suffix), a dotted module name
  ("modules.tailnet.run", `-m …`), a folder read as a whole. A bare name counts only next to the file that names it (a
  test's own "README.md" is a fixture, not ours), and a sentence that mentions a file does not count. A test's references
  are always followed (a test that runs `bin/hub` depends on what `bin/hub` imports); another file's only when few tests
  reach it (a page server is reached by the page's tests; `core/i18n.py` names the translations folder and nearly every
  test imports it — following that would make one translation file select the whole suite). A reference to a file inside a
  folder never reaches Python there: code in a folder is loaded by scanning, which a static graph cannot follow.

Always selected ("sentinels"): a test module that reads the tree without a fixed path — `os.walk`/`glob`/`listdir`/
`rglob`/`iterdir`/`git ls-files|grep` rooted at the repo where the folder is not a constant (a loop variable, the
root itself). Such a test checks every file, so any change can break it. A walk of a fixed folder is a dependency on
that folder, not a sentinel.

Run everything (with the reason printed): `tools/test.py`, `tests/__init__.py`, `tests/fakes`, the language data
(`i18n/data/`), any other file in the
tests' root that is not a test, the test lock/temp helpers, a file of unknown type, too many changed files, no
git or no base. Every doubt goes this way."""
import ast
import json
import os
import re
import subprocess
import sys

MANY = 60                # changed files above this: run everything
FEW = 0.15               # code imported by at most this share of test modules passes its data edges on

FULL_FILES = {"tools/test.py", "tests/__init__.py", "core/testlock.py", "core/testtmp.py"}
FULL_PREFIXES = ("tests/fakes/",)
# the words and earlier forms the core recognises (core/langdata.py): read by labels, the fold, the watcher — like code
# nearly every test imports, which the graph does not follow from a data file
LANG_DATA = ("i18n/data/",)
# The page in Chrome: its tests (and every module that builds on them) run when what the page serves changes — its own
# folder's non-code files and the translations, which the server substitutes into the page.
PAGE_E2E = ("tests/test_web_e2e.py", "tests/test_files_e2e.py")
PAGE_DATA = ("modules/web/", "modules/files/", "i18n/")
NAMES_PATHS = ("core/testsel.py",)   # names paths as rules, depends on none of them
CODE_EXT = (".py", ".mjs")
JS_EXT = (".js", ".mjs")
TEXT_EXT = (".js", ".mjs", ".css", ".html")
KNOWN_EXT = {".py", ".json", ".js", ".md", ".css", ".sh", ".png", ".svg", ".woff2", ".html", ".txt", ".webm", ".toml",
             ".service", ".ps1", ".plist", ".mjs", ".jsonl", ".icns", ".c", ".gitignore", ".jpg", ".jpeg", ".gif",
             ".yml", ".yaml", ".mp4", ".woff", ".ttf", ".ico", ".webp", ".conf"}
KNOWN_NAMES = {"Dockerfile", ".gitignore", ".gitleaks.toml", ".publish-deny", ".publish-deny-case", "LICENSE",
               "NOTICE"}
TOP_CODE = ("core", "modules", "hosts", "hooks", "platforms", "tests", "tools", "bin", "observers", "evals", "relay")
WALKS = {"walk", "listdir", "scandir", "glob", "iglob", "rglob", "iterdir"}


# ---- a source of files: the working tree or a commit (to replay history without checking it out) -------------------

class Disk:
    def __init__(self, root):
        self.root = root

    def files(self):
        """{path: key}; the key changes when the content does (the cache of per-file facts is keyed by it)."""
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=self.root,
                             capture_output=True).stdout.decode("utf-8", "replace").split("\0")
        res = {}
        for p in out:
            full = os.path.join(self.root, p)
            if p and os.path.isfile(full):
                st = os.stat(full)
                res[p] = (st.st_mtime_ns, st.st_size)
        return res

    def read(self, path):
        try:
            with open(os.path.join(self.root, path), "rb") as f:
                return f.read()
        except OSError:
            return b""


class Commit:
    def __init__(self, root, rev):
        self.root, self.rev = root, rev

    def files(self):
        out = subprocess.run(["git", "ls-tree", "-r", "-z", self.rev], cwd=self.root, capture_output=True
                             ).stdout.decode("utf-8", "replace").split("\0")
        res = {}
        for line in out:
            if line and "\t" in line:
                meta, p = line.split("\t", 1)
                parts = meta.split()
                if parts[1] == "blob":
                    res[p] = parts[2]
        return res

    def read(self, path):
        r = subprocess.run(["git", "cat-file", "blob", f"{self.rev}:{path}"], cwd=self.root, capture_output=True)
        return r.stdout if r.returncode == 0 else b""


# ---- facts of one file (independent of the others, cached by content) ---------------------------------------------------

def is_script(data):
    """A file without .py that Python runs: the shebang."""
    first = data[:200].split(b"\n", 1)[0]
    return first.startswith(b"#!") and b"python" in first


def ext_of(path):
    base = path.rsplit("/", 1)[-1]
    return "" if "." not in base.lstrip(".") and not base.startswith(".") else "." + base.rsplit(".", 1)[-1]


def is_python(path, data):
    return path.endswith(".py") or ("." not in path.rsplit("/", 1)[-1] and is_script(data))


class _Ev:
    """Partial evaluation of path expressions. A path is a list of parts: ("@", n) — the folder of the file
    after n-1 `dirname` (n=0: the file itself, 1: its folder, 2: the root of a test file); str — a constant; None — unknown."""

    def __init__(self, tree):
        self.env, self.busy, self._seen = {}, set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self._put(t.id, node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                self._put(node.target.id, node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                pos = a.posonlyargs + a.args
                for arg, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
                    self._put(arg.arg, d)
                for arg, d in zip(a.kwonlyargs, a.kw_defaults):
                    if d is not None:
                        self._put(arg.arg, d)
            elif isinstance(node, (ast.For, ast.comprehension)):
                it = node.iter
                tgt = node.target
                if isinstance(tgt, ast.Name) and isinstance(it, (ast.Tuple, ast.List)):
                    [self._put(tgt.id, e) for e in it.elts]

    def _put(self, name, expr):
        """Remember an assignment; the same expression again adds nothing (a name is reused in many functions)."""
        key = (name, ast.dump(expr))
        if key not in self._seen:
            self._seen.add(key)
            self.env.setdefault(name, []).append(expr)

    def ev(self, n, depth=0):
        """A list of alternatives (each a list of parts) or [] when the expression is not a path."""
        if depth > 14:
            return [[None]]
        if isinstance(n, ast.Constant):
            return [[n.value]] if isinstance(n.value, str) else []
        if isinstance(n, ast.Name):
            if n.id == "__file__":
                return [[("@", 0)]]
            if n.id in self.busy:
                return []   # `root = root or ROOT`: the name inside its own definition adds nothing
            self.busy.add(n.id)
            try:
                alts = []
                for e in self.env.get(n.id, []):
                    alts += self.ev(e, depth + 1)
            finally:
                self.busy.discard(n.id)
            return _top(alts) or [[None]]
        if isinstance(n, ast.JoinedStr):
            text, anchors = "", []
            for v in n.values:
                if isinstance(v, ast.Constant):
                    text += str(v.value)
                    continue
                sub = self.ev(v.value, depth + 1) if isinstance(v, ast.FormattedValue) else []
                if sub and sub[0] and isinstance(sub[0][0], tuple) and len(sub[0]) == 1 and not text:
                    anchors.append(sub[0][0])
                    text += "\x01"
                else:
                    text += "\x00"
            out = []
            for piece in text.replace("\\", "/").split("/"):
                if piece == "\x01":
                    out.append(anchors.pop(0))
                elif piece.startswith("\x01"):
                    anchors.pop(0)
                    out.append(None)
                elif "\x00" in piece:
                    out.append(None)
                elif piece:
                    out.append(piece)
            return [out] if out else []
        if isinstance(n, ast.BoolOp):
            return _top([a for v in n.values for a in self.ev(v, depth + 1)])
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            return self._cat([n.left, n.right], depth)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            return self._cat([n.left, n.right], depth)
        if isinstance(n, ast.Call):
            name = _callee(n)
            if name == "join" and n.args and _is_path_join(n):
                return self._cat(n.args, depth)
            if name in ("dirname", "parent") and n.args:
                return [self._up(p) for p in self.ev(n.args[0], depth + 1)]
            if name in ("Path", "PurePath", "PosixPath") and n.args:
                return self._cat(n.args, depth)
            if name in ("abspath", "realpath", "resolve", "normpath", "str", "fspath", "expanduser") and n.args:
                return self.ev(n.args[0], depth + 1)
            if name in ("resolve", "absolute") and isinstance(n.func, ast.Attribute):
                return self.ev(n.func.value, depth + 1)
            return [[None]]
        if isinstance(n, ast.Attribute):
            if n.attr == "parent":
                return [self._up(p) for p in self.ev(n.value, depth + 1)]
            return [[None]]
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and n.value.attr == "parents" \
                and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, int):
            return [self._up(p, n.slice.value + 1) for p in self.ev(n.value.value, depth + 1)]
        return [[None]]

    def _up(self, p, k=1):
        for _ in range(k):
            if p and isinstance(p[0], tuple) and len(p) == 1:
                p = [("@", p[0][1] + 1)]
            elif len(p) > 1:
                p = p[:-1]
            else:
                return [None]
        return p

    def _cat(self, args, depth):
        """Join: constants split into folders (`"a/b"` is two), unknown parts stay None."""
        alts = [[]]
        for a in args:
            sub = self.ev(a, depth + 1) or [[None]]
            alts = _top([x + y for x in alts for y in sub])
        out = []
        for x in alts:
            parts = []
            for p in x:
                if isinstance(p, str):
                    parts += [q for q in p.replace("\\", "/").split("/") if q and q != "."]
                else:
                    parts.append(p)
            out.append(parts)
        return out


def _is_path_join(call):
    """os.path.join(...) / posixpath.join / a bare `join` — not `", ".join(items)`."""
    f = call.func
    if isinstance(f, ast.Name):
        return f.id == "join"
    return isinstance(f, ast.Attribute) and f.attr == "join" and (
        (isinstance(f.value, ast.Attribute) and f.value.attr == "path")
        or (isinstance(f.value, ast.Name) and f.value.id in ("path", "posixpath", "ntpath", "os")))


def _top(alts, cap=16):
    """At most `cap` alternatives, the ones rooted at a known folder first (they are the ones that name repo files)."""
    return sorted(alts, key=lambda p: not (p and isinstance(p[0], tuple)))[:cap]


def _callee(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def python_facts(path, data):
    try:
        tree = ast.parse(data.decode("utf-8", "replace"))
    except (SyntaxError, ValueError, RecursionError):
        return {"bad": True, "imports": [], "strs": [], "paths": [], "walks": [], "ntests": 0}
    imports, strs, paths, walks = [], set(), [], []
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    ev = _Ev(tree)
    inner = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [(a.name, 0, []) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.module or "", node.level, [a.name for a in node.names]))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs \
                and 2 <= len(node.value) <= 400:
            strs.add(node.value)
        if isinstance(node, ast.Call):
            walks += _walks(node, ev)
        if id(node) not in inner and ((isinstance(node, ast.Call) and (_is_path_join(node) or _callee(node) in ("Path", "PurePath")))
                                      or isinstance(node, ast.JoinedStr)
                                      or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div))):
            # the whole expression only: the prefix of `ROOT / "a" / "b"` is not a separate reference to "a"
            inner |= {id(x) for x in ast.walk(node) if x is not node}
            paths += [p for p in ev.ev(node) if any(isinstance(x, str) and x for x in p)
                      and (isinstance(p[0], tuple) or len(p) > 1)]
    ntests = sum(1 for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name.startswith("test"))
    return {"bad": False, "imports": imports, "strs": sorted(strs), "paths": paths, "walks": walks, "ntests": ntests}


GIT_WORDS = {"git", "ls-files", "grep", "-z", "--cached", "--others", "--exclude-standard", "-C", "-c", "--", "-l", "-n",
             "-I", "-e", "-F", "-E", "-w", "-i", "--ignored", "--recurse-submodules"}


def _walks(call, ev):
    """The tree reads of one call: [("tree"|"dir"|"unknown", parts)]. "unknown" — a root nobody fixed in this file (a
    parameter): a test's own temp folders are such, so only a tool file's counts."""
    name = _callee(call)
    out = []
    if name in ("run", "check_output", "Popen", "check_call", "call"):
        words = [a.value for a in ast.walk(call) if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if "git" in words and ("ls-files" in words or "grep" in words):
            roots = [k.value for k in call.keywords if k.arg == "cwd"]
            elts = [e for a in call.args[:1] if isinstance(a, (ast.List, ast.Tuple)) for e in a.elts]
            for i, e in enumerate(elts[:-1]):
                if isinstance(e, ast.Constant) and e.value == "-C":
                    roots.append(elts[i + 1])
            specs = [w for w in words if w not in GIT_WORDS and not w.startswith("-")]
            for e in roots or [None]:
                for p in (ev.ev(e) if e is not None else [[None]]):
                    out.append(("dir" if specs else "tree", p + specs[:1]) if p and isinstance(p[0], tuple)
                               else ("unknown", p))
        return out
    if name not in WALKS:
        return out
    if name in ("iterdir", "rglob", "glob") and isinstance(call.func, ast.Attribute) and not (
            name == "glob" and isinstance(call.func.value, ast.Name) and call.func.value.id in ("glob", "iglob")):
        extra = [a.value for a in call.args[:1] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        for p in ev.ev(call.func.value):
            out.append(("dir", p + extra) if p and isinstance(p[0], tuple) else ("unknown", p))
        return out
    if call.args:
        for p in ev.ev(call.args[0]):
            out.append(("dir", p) if p and isinstance(p[0], tuple) else ("unknown", p))
    return out


def text_facts(path, data):
    """Page scripts, styles, html: ES-module imports and quoted paths."""
    text = data.decode("utf-8", "replace")[:3_000_000]
    toks = set(re.findall(r"""['"(=]\s*([\w./@-]+\.(?:js|mjs|css|html|svg|png|json|woff2|webm))""", text))
    rel = set(re.findall(r"""(?:from|import)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""", text))
    return {"toks": sorted(toks), "rel": sorted(rel)}


# ---- the graph ---------------------------------------------------------------------------------------------------------

class Graph:
    def __init__(self, source, cache=None):
        self.source = source
        keys = source.files()
        self.files = sorted(keys)
        self.fileset = set(self.files)
        cache = {} if cache is None else cache
        self.code = {}      # path -> True for files Python/JS runs
        facts = {}
        for p in self.files:
            ck = (p, keys[p])
            if ck not in cache:
                data = source.read(p) if (p.endswith(CODE_EXT + TEXT_EXT) or "." not in p.rsplit("/", 1)[-1]) else b""
                if is_python(p, data):
                    cache[ck] = ("py", python_facts(p, data))
                elif p.endswith(TEXT_EXT):
                    cache[ck] = ("text", text_facts(p, data))
                else:
                    cache[ck] = ("none", None)
            kind, f = cache[ck]
            if kind != "none":
                facts[p] = (kind, f)
            self.code[p] = kind == "py"
        self.facts = facts
        self.tests = [p for p in self.files if re.fullmatch(r"tests/test_[^/]+\.py", p)]
        self.dirs = {}
        for p in self.files:
            d = p
            while "/" in d:
                d = d.rsplit("/", 1)[0]
                self.dirs.setdefault(d, []).append(p)
        self.by_base = {}       # file name -> paths (a script importing a neighbour found through sys.path)
        self.by_suffix = {}     # "static/js/x.js", "js/x.js" -> paths
        for p in self.files:
            parts = p.split("/")
            self.by_base.setdefault(parts[-1], []).append(p)
            for i in range(1, len(parts) - 1):
                self.by_suffix.setdefault("/".join(parts[i:]), []).append(p)
        self.tops = {p.split("/")[0] for p in self.files if "/" in p and p.endswith(".py")}   # first-party packages
        self.imp_edges, self.ref_edges = {}, {}
        self.sentinels = {}
        self.closures = {}
        self._local = {}
        self._build()
        self.rev = self.edges()

    # ---- resolution
    def module_files(self, dotted, level=0, base=None, names=()):
        """Files a Python import runs: the packages on the way, the module, and submodules named in `from x import y`."""
        parts = dotted.split(".") if dotted else []
        if level:
            pkg = base.rsplit("/", 1)[0] if "/" in base else ""
            for _ in range(level - 1):
                pkg = pkg.rsplit("/", 1)[0] if "/" in pkg else ""
            prefix = [x for x in pkg.split("/") if x]
        else:
            if parts and parts[0] not in self.tops and parts[0] not in self.local_names(base):
                # a bare name from a folder put on sys.path (tests import tools/ and evals/ that way)
                found = [] if parts[0] in sys.stdlib_module_names else self.by_base.get(parts[0] + ".py", [])
                return [q for q in found if q.split("/")[0] in ("tools", "evals", "relay")][:2]
            prefix = [] if parts and parts[0] in self.tops else base.rsplit("/", 1)[0].split("/") if "/" in base else []
        out = []
        cur = list(prefix)
        top = bool(level) or bool(parts) and parts[0] in self.tops
        for part in parts:
            cur.append(part)
            found = [c for c in ("/".join(cur) + ".py", "/".join(cur) + "/__init__.py") if c in self.fileset]
            out += found
        if top and parts and not found:
            out.append("/".join(cur) + ".py")   # a missing file (deleted or renamed): the importer still names it
        for nm in names:
            found = [c for c in ("/".join(cur + [nm]) + ".py", "/".join(cur + [nm]) + "/__init__.py")
                     if c in self.fileset]
            out += found
            if top and not found and "/".join(cur) + "/__init__.py" in self.fileset:
                out.append("/".join(cur + [nm]) + ".py")   # a name that may be a deleted submodule; harmless if it is not
        return out

    def local_names(self, base):
        """A bare import resolved against the folder of the file (scripts import their neighbours)."""
        d = base.rsplit("/", 1)[0] if "/" in base else ""
        if d not in self._local:
            names = set()
            for p in self.dirs.get(d, []) if d else self.files:
                rest = p[len(d) + 1:] if d else p
                if rest.endswith(".py") and "/" not in rest:
                    names.add(rest[:-3])
                elif rest.endswith("/__init__.py") and rest.count("/") == 1:
                    names.add(rest.split("/")[0])
            self._local[d] = names
        return self._local[d]

    def anchor(self, base, n):
        """The folder of the file `base` after n-1 `dirname`: n=1 its folder, n=2 its parent…"""
        parts = base.split("/")[:-1]
        k = n - 1
        if k > len(parts):
            return None
        return parts[:len(parts) - k] if k else parts

    def target_of(self, base, p):
        """A path expression (list of parts) as (path, exact): repo-relative or None. A part None cuts it there."""
        if not p or not isinstance(p[0], tuple):
            return None
        a = self.anchor(base, p[0][1])
        if a is None:
            return None
        out, exact = list(a), True
        for x in p[1:]:
            if x is None or isinstance(x, tuple):   # unknown, or a second root: the known prefix is all we have
                exact = False
                break
            if x == "..":
                if out:
                    out.pop()
                continue
            if x in ("", "."):
                continue
            out.append(x)
        return "/".join(out), exact

    def _as_files(self, path):
        """The files a repo path names: itself, or a folder's whole subtree."""
        if path in self.fileset:
            return [path]
        if path in self.dirs:
            return list(self.dirs[path])
        return []

    def _token_files(self, base, tok):
        """A quoted string that may name a file: path from the root, from the file's folders up, a suffix, a bare name."""
        tok = tok.strip().replace("\\", "/")
        while tok.startswith("./"):
            tok = tok[2:]
        tok = tok.lstrip("/")
        if not tok or len(tok) < 3 or " " in tok:
            return []
        if tok in self.fileset and "/" in tok:
            return [tok]
        d = base.rsplit("/", 1)[0] if "/" in base else ""
        while True:   # relative to the file's folder and the ones above
            cand = f"{d}/{tok}" if d else tok
            if cand in self.fileset and (d or "/" in tok):
                return [cand]
            if "/" not in tok and d and cand in self.dirs and not any(x.endswith(".py") for x in self.dirs[cand]):
                return list(self.dirs[cand])
            if not d:
                break
            d = d.rsplit("/", 1)[0] if "/" in d else ""
        if tok in self.by_suffix and "/" in tok:
            return list(self.by_suffix[tok])
        if "/" in tok:
            if tok.rstrip("/") in self.dirs:
                return list(self.dirs[tok.rstrip("/")])
            return []
        return []

    def _add(self, src, tgt, named=True, strong=False):
        """An edge: strong (an import — always followed) or a reference (a path or name in a string — followed from tests
        and from code few tests import). A reference to a file inside a folder the source names (not named) skips Python:
        code in a folder is loaded by scanning, which a static graph cannot follow."""
        if tgt == src or src in NAMES_PATHS and not strong or (not named and self.code.get(tgt, tgt.endswith(CODE_EXT))):
            return
        if tgt.startswith("tests/") and not src.startswith("tests/") and not strong:
            return   # a runner that names test modules by string does not depend on them
        (self.imp_edges if strong else self.ref_edges).setdefault(src, set()).add(tgt)

    def _build(self):
        for p, (kind, f) in self.facts.items():
            if kind == "text":
                for r in f["rel"]:
                    base = p.rsplit("/", 1)[0] if "/" in p else ""
                    q = os.path.normpath(f"{base}/{r}" if base else r).replace("\\", "/")
                    for cand in (q, q + ".js"):
                        if cand in self.fileset:
                            self._add(p, cand, strong=True)
                for t in f["toks"]:
                    for q in self._token_files(p, t):
                        self._add(p, q, named=q == t or q.endswith("/" + t))
                continue
            if f["bad"]:
                continue
            for dotted, level, names in f["imports"]:
                for q in self.module_files(dotted, level, p, names):
                    self._add(p, q, strong=True)
            for s in f["strs"]:
                if re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+", s) and s.split(".")[0] in self.tops:
                    whole = "/".join(s.split("."))
                    if whole + ".py" in self.fileset or whole + "/__init__.py" in self.fileset:   # not "modules.json"
                        for q in self.module_files(s, 0, p):
                            self._add(p, q)
                    continue
                # a whole string that is a path (not a sentence that mentions one)
                tok = s.strip().replace("\\", "/")
                if re.fullmatch(r"\.{0,2}/?[\w.@-]+(?:/[\w.@-]+)*/?", tok):
                    for q in self._token_files(p, tok):
                        if "/" in tok or not q.endswith(".py"):
                            self._add(p, q, named=q == tok or q.endswith("/" + tok))
                for dotted in re.findall(r"-m\s+([A-Za-z_][\w.]*)", s):
                    for q in self.module_files(dotted, 0, p):
                        self._add(p, q)
            for pe in f["paths"]:
                t = self.target_of(p, pe)
                if t:
                    path, exact = t
                    for q in self._as_files(path):
                        self._add(p, q, named=q == path)
                    if exact and path not in self.fileset and path not in self.dirs and path:
                        self._add(p, path)   # a named file that is gone (deleted, renamed)
            for kind2, pe in f["walks"]:
                self._walk(p, kind2, pe)

    def _walk(self, p, kind, pe):
        if kind == "unknown":
            return
        if not pe or not isinstance(pe[0], tuple):
            return
        t = self.target_of(p, pe)
        if t is None:
            return
        path, exact = t
        # a glob pattern cuts the folder at the first wildcard
        cut = []
        for seg in (path.split("/") if path else []):
            if any(c in seg for c in "*?["):
                exact = False
                break
            cut.append(seg)
        folder = "/".join(cut)
        if folder in self.fileset:
            return
        files = self.dirs.get(folder, [])
        if kind == "tree" or folder == "" or folder in TOP_CODE:
            self.sentinels.setdefault(p, "walks the whole repo tree" if not folder else f"walks {folder}/ as a whole")
            return
        for q in files:
            self._add(p, q, named=True)   # a fixed folder read as a whole: what is in it, code included, is its input

    # ---- selection
    def forward(self, start):
        """What a test reaches: imports everywhere, references only from files of the tests' own folder."""
        seen, stack = {start}, [start]
        while stack:
            cur = stack.pop()
            nxt = set(self.imp_edges.get(cur, ()))
            if cur.startswith("tests/"):
                nxt |= self.ref_edges.get(cur, set())
            for q in nxt:
                if q not in seen:
                    seen.add(q)
                    stack.append(q)
        return seen

    def edges(self):
        """Reversed edges that count: every import; a reference from a test file, or from code that few tests reach (a page
        server is reached by the page's tests; `core/i18n.py` by nearly all, so a translation file selects only the tests that
        name it themselves)."""
        count, closures = {}, self.closures
        for t in self.tests:
            closures[t] = self.forward(t)
            for q in closures[t]:
                count[q] = count.get(q, 0) + 1
        self.reach = count
        limit = max(1, int(FEW * len(self.tests)))
        # a dev tool that walks the tree (tools/i18n.py) makes its few importers sentinels too; a walker everyone imports
        # (core/catalog.py scans the modules folder) does not — that would make every test a sentinel
        self.walkers = {}
        for t in self.tests:
            if t in self.sentinels:
                self.walkers[t] = self.sentinels[t]
                continue
            for q in sorted(closures[t]):
                if q in self.sentinels and not q.startswith("tests/") and count[q] <= limit:
                    self.walkers[t] = f"through {q}: {self.sentinels[q]}"
                    break
        rev = {}
        for src, tgts in self.imp_edges.items():
            for q in tgts:
                rev.setdefault(q, set()).add(src)
        for src, tgts in self.ref_edges.items():
            if src.startswith("tests/") or count.get(src, 0) <= limit:
                for q in tgts:
                    rev.setdefault(q, set()).add(src)
        return rev

    def affected(self, changed):
        """{test module: chain from the test to the changed file} for the tests whose dependencies include a change."""
        rev = self.rev
        parent = {}
        queue = []
        for c in sorted(changed):
            parent[c] = None
            queue.append(c)
        i = 0
        while i < len(queue):
            cur = queue[i]
            i += 1
            for s in sorted(rev.get(cur, ())):
                if s not in parent:
                    parent[s] = cur
                    queue.append(s)
        out = {}
        for t in self.tests:
            if t in parent:
                chain, x = [], t
                while x is not None:
                    chain.append(x)
                    x = parent[x]
                out[t] = chain
        return out


# ---- the diff ----------------------------------------------------------------------------------------------------------

def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def default_base(root):
    """merge-base of HEAD with the main branch. On main itself (or when local main already has the work) that is HEAD
    and the commits would not count: then the remote main, if it is further back."""
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    found = None
    for b in ("main", "origin/main", "master"):
        r = git(root, "merge-base", "HEAD", b)
        if r.returncode == 0 and r.stdout.strip():
            if r.stdout.strip() != head:
                return r.stdout.strip()
            found = found or r.stdout.strip()
    return found


def changed_files(root, base):
    """Paths changed since base: commits and the working tree, plus new untracked files (not ignored)."""
    if base.startswith("-"):
        return None   # a base is a commit or a branch, never an option for git
    r = git(root, "diff", "--name-only", "--no-renames", "-z", base)
    if r.returncode != 0:
        return None
    names = {x for x in r.stdout.split("\0") if x}
    names |= {x for x in git(root, "ls-files", "-z", "--others", "--exclude-standard").stdout.split("\0") if x}
    return sorted(names)


def doubt(path, graph):
    """Why this changed file forces a full run, or None."""
    if path in FULL_FILES:
        return f"{path} — a shared helper of the test run"
    if path.startswith(FULL_PREFIXES):
        return f"{path} — fakes many tests use"
    if path.startswith(LANG_DATA):
        return f"{path} — language data the core reads everywhere (core/langdata.py)"
    if re.fullmatch(r"tests/[^/]+", path) and not re.fullmatch(r"tests/test_[^/]+\.py", path):
        return f"{path} — a helper at the root of tests/"
    base = path.rsplit("/", 1)[-1]
    if base in KNOWN_NAMES:
        return None
    ext = ext_of(path)
    if ext in KNOWN_EXT:
        return None
    if ext == "":
        data = graph.source.read(path) if path in graph.fileset else b""
        if data[:2] == b"#!":
            return None
    return f"{path} — an unknown file type"


def select(root, base=None, source=None, changed=None, graph=None, cache=None):
    """The answer: {"full": reason or None, "base", "changed", "tests": {module: why}, "sentinels": {module: why},
    "all": number of test modules}. `changed`/`source`/`graph` let the history replay run the same code."""
    if changed is None:
        base = base or default_base(root)
        if not base:
            return {"full": "no base to compare with (no main branch)", "base": None, "changed": [], "tests": {},
                    "sentinels": {}, "all": 0}
        changed = changed_files(root, base)
        if changed is None:
            return {"full": f"could not compare with {base}", "base": base, "changed": [], "tests": {},
                    "sentinels": {}, "all": 0}
    graph = graph or Graph(source or Disk(root), cache)
    res = {"full": None, "base": base, "changed": changed, "tests": {}, "sentinels": {}, "all": len(graph.tests),
           "uncovered": [], "chrome": []}
    if not changed:
        return res
    if len(changed) > MANY:
        res["full"] = f"{len(changed)} files changed (more than {MANY})"
        return res
    for c in changed:
        why = doubt(c, graph)
        if why:
            res["full"] = why
            return res
        if c in graph.facts and graph.facts[c][0] == "py" and graph.facts[c][1]["bad"]:
            res["full"] = f"{c} — does not parse as Python"
            return res
    hit = graph.affected(set(changed))
    for t, chain in hit.items():
        res["tests"][t] = chain
    page = [c for c in changed if c.startswith(PAGE_DATA) and not c.endswith(".py")]
    if page:
        for t in graph.tests:
            if t not in res["tests"] and any(e in graph.closures[t] for e in PAGE_E2E):
                res["tests"][t] = [t, "the page in Chrome", page[0]]
    for t, why in graph.walkers.items():
        if t not in res["tests"]:
            res["sentinels"][t] = why
    used = {chain[-1] for chain in res["tests"].values()}
    res["uncovered"] = [c for c in changed if c not in used]
    res["chrome"] = sorted(t for t in {**res["tests"], **res["sentinels"]}
                           if any(e in graph.closures[t] for e in PAGE_E2E))
    return res


def heavy(res):
    """Does the selection need the machine's test lock: a page test in Chrome (the memory the lock guards) or a third of
    the suite. A small selection runs beside someone else's full run, as a run with names always did."""
    return bool(res["chrome"]) or len(res["tests"]) + len(res["sentinels"]) > res["all"] // 3


def module_name(path):
    return path[:-3].replace("/", ".")


def describe(chain):
    """One line: why a test is selected (the changed file and the way to the test)."""
    if len(chain) == 1:
        return "changed itself"
    if len(chain) == 2:
        return f"depends on {chain[1]}"
    return f"depends on {chain[-1]} (through {chain[1]})" if len(chain) == 3 else \
        f"depends on {chain[-1]} (through {chain[1]} … {len(chain) - 2} steps)"


def report(res, limit=40):
    """The lines `tools/test.py --changed` prints: what was compared, what is selected and why."""
    out = []
    base = res["base"][:9] if res["base"] else "—"
    out.append(f"--changed: base {base}, files changed: {len(res['changed'])}")
    if res["full"]:
        out.append(f"full run: {res['full']}")
        return out
    if not res["changed"]:
        out.append("no changes — nothing to run")
        return out
    tests, sentinels = res["tests"], res["sentinels"]
    out.append(f"test modules selected: {len(tests) + len(sentinels)} of {res['all']} "
               f"(by dependencies {len(tests)}, sentinels {len(sentinels)})")
    rows = [f"  {module_name(t)} — {describe(c)}" for t, c in sorted(tests.items())]
    rows += [f"  {module_name(t)} — sentinel: {why}" for t, why in sorted(sentinels.items())]
    out += rows[:limit]
    if len(rows) > limit:
        out.append(f"  … and {len(rows) - limit} more")
    if res["uncovered"]:
        out.append("no test depends on: " + ", ".join(res["uncovered"][:10])
                   + (f" … and {len(res['uncovered']) - 10} more" if len(res["uncovered"]) > 10 else ""))
    return out
