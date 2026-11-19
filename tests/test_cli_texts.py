"""The `hub` commands in the texts sessions read are commands the real bin/hub takes (13.11: 173 refusals of `hub` by its
parameters in a week; the critic's rule said `[--evidence <paths>]` and 39 runs gave the paths separated by spaces; the
help's flag list was held to the code, the texts were held to nothing).

Every `hub …` in backticks of the session's texts — hub-home/CLAUDE.md, the skills of plugins/, the core's addenda in
hosts/, the critic's rule, the core's phrases (i18n/en.json) — and every example of core/hubhelp.txt goes through
bin/hub's own parsing (tests/cliprobe.py: it stops at the first thing the command would do). A form with placeholders is
filled from a fixture fleet first: `<name>` is a session, `dN` its task, `[…]` is taken, `a|b` is `a`. A bare mention
(`hub send`, `hub task run …`) is held only to the names: the command and its subcommand exist."""
import tests  # noqa: F401 — ignore the machine's settings
import ast
import glob
import json
import os
import re
import shlex
import tempfile
import unittest

from core import clihelp
from tests import cliprobe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK = re.compile(r"`(hub [^`\n]+)`")
# the hub's own texts: their commands are the hub's
HUB_TEXTS = ("hub-home/", "plugins/hub-coordinator/")


def sources():
    """(where, text) of what sessions read."""
    out = []
    for path in ["hub-home/CLAUDE.md", *sorted(glob.glob(os.path.join(REPO, "plugins/*/skills/**/*.md"), recursive=True))]:
        path = os.path.relpath(os.path.join(REPO, path), REPO)
        with open(os.path.join(REPO, path), encoding="utf-8") as f:
            out.append((path, f.read()))
    for path in sorted(glob.glob(os.path.join(REPO, "hosts/*.py"))) + [os.path.join(REPO, "modules/critic/critic.py")]:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        texts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        out.append((os.path.relpath(path, REPO), "\n".join(texts)))
    from modules.critic import critic
    out.append(("critic.NOTE", critic.NOTE))
    with open(os.path.join(REPO, "i18n", "en.json"), encoding="utf-8") as f:
        out += [(f"i18n:{k}", v) for k, v in json.load(f).items() if isinstance(v, str)]
    return out


def spans():
    """(where, the command text) — backticked `hub …` and the help's examples."""
    got = [(where, m.group(1)) for where, text in sources() for m in TICK.finditer(text)]
    got += [("core/hubhelp.txt", x) for e in clihelp.load().entries for x in e.examples]
    return got


def fill(span, names):
    """The words of a span as a command line, its placeholders filled; None — a text the shell could not split."""
    text = re.sub(r"\{(\w+)\}", r"<\1>", span)                    # i18n fields
    text = re.sub(r"([\"'])(…|\.\.\.)\1", r"\1a text\1", text)            # "…" is a text
    text = re.sub(r"\s+(<<'?EOF'?|2>&1).*$", "", text)             # a heredoc or a redirection after the command
    text = re.sub(r"<([^<>]*)>", lambda m: "<" + m.group(1).replace(" ", "_") + ">", text)   # one word each
    text = re.sub(r"\[([^\[\]]*)\]", r"\1", text)                  # optional parts: take them
    text = re.sub(r"\[([^\[\]]*)\]", r"\1", text)                  # (nested once)
    try:
        words = shlex.split(text)
    except ValueError:
        return None
    out = []
    for i, w in enumerate(words[1:]):
        prev = out[-1] if out else ""
        if w in ("…", "...") or w.startswith("#"):
            break
        if "|" in w and not w.startswith(("<", '"')) and " " not in w:
            w = w.split("|")[0]
        lw = w.strip("<>").lower()
        if prev in ("--about",) or re.fullmatch(r"<?d(n|…|\d*)>?", w, re.I):
            w = names["task"]
        elif re.fullmatch(r"<?m(n|\d+)>?", w, re.I):
            w = "m1"
        elif re.fullmatch(r"<?q(n|\d+)>?", w, re.I):
            w = "q1"
        elif w in ("N", "<N>", "S"):
            w = "2"
        elif w.startswith("<") and w.endswith(">"):
            w = PLACE.get(lw.split()[0].rstrip(".…:"), names["executor"])
        out.append(w)
    if out and out[-1].startswith("-") and out[-1] in VALUED:   # "use `hub show --grep`": the flag is named, not given
        out.append("x")
    return out


VALUED = {f for e in clihelp.load().entries for f in e.value_flags()}
PLACE = {"file": "/tmp/x.md", "draft": "/tmp/x.md", "path": "/tmp/x.md", "paths": "/tmp/x.md", "report": "/tmp/x.md",
         "png": "/tmp/x.png", "dir": "/tmp", "folder": "/tmp", "id": "browser:mac", "slug": "maps", "text": "a text",
         "what": "a text", "why": "a reason", "kind": "normal", "gist": "maps-fix", "who": "Masha", "session": "maps",
         "to": "maps"}


class Texts(unittest.TestCase):
    def test_every_command_in_a_text_for_sessions_is_taken_by_bin_hub(self):
        h = clihelp.load()
        names = cliprobe.names()
        jobs, where = [], []
        for src, span in spans():
            argv = fill(span, names)
            if not argv or argv[0] not in h.tops():
                continue
            exact, subs, key = h.find(argv)
            if " ".join(argv) == key or not exact and subs or len(argv) <= 2 and not re.search(r"[\"'<]|--", span):
                continue   # a mention: `hub task run`, `hub session`, "tell it with `hub send hub`"
            jobs.append({"argv": argv, "me": "hub" if src.startswith(HUB_TEXTS) else names["mini"]})
            where.append((src, span))
        got = cliprobe.run(jobs)
        bad = [f"{src}: `{span}` → {verdict}: {text.splitlines()[0] if text else ''}"
               for (src, span), (verdict, text), job in zip(where, got, jobs)
               if verdict in ("form", "crash") or verdict == "help" and "--help" not in job["argv"]]
        self.maxDiff = None
        self.assertEqual(bad, [])
        self.assertGreater(len(jobs), 100)


if __name__ == "__main__":
    unittest.main()
