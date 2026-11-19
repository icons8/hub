"""Parse a `hub` command line with the real bin/hub, doing nothing (13.11: the commands in the texts sessions read were held
only to the help's flag list, never to the parser; the forms of values — a list, `--about dN` — went unchecked).

    python3 tests/cliprobe.py < argvs.json   →  [[verdict, what], …] on stdout

The probe loads bin/hub into its own process (a state folder, hub.json and settings folder of its own, from a session),
on a fixture state: a hub, a mini-hub `notes` with its executor `notes-ci` asleep after handing in its task, and the
sessions `docs`, `api-x`. A fact the command would put into the core's mailbox, a program it would start, a request it
would send — each stops the command: that is "ok" (its words were taken). A refusal before that — "refused" and its
text; the help printed — "help". The core's own checks (who may, the state) are not run: those are the reducer's.
"""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")


class Took(Exception):
    """The command got as far as doing something: its words were parsed."""


def fleet():
    """The fixture state and the names in it: {"mini": …, "executor": …, "task": dN of the executor's handed-in task}."""
    sys.path.insert(0, REPO)
    from tests.test_minihub import Wake
    w = Wake()
    w.setUp()
    w.minihub()
    did, mid = w.task()
    sub = w.done_and_asleep(did, mid)
    for name in ("maps", "geo"):   # the names the help's examples use
        w.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", channel="cli", owner=True)
        w.ev("session.started", name)
    return w.st, {"mini": "notes", "executor": "notes-ci", "task": sub["id"], "parent": did, "message": mid}


def load(state):
    """bin/hub as a module, on its own folders (the environment is set before it reads it at import)."""
    import importlib.machinery
    import importlib.util
    os.environ.update(HUB_STATE_DIR=state, HUB_CONFIG=os.path.join(state, "hub.json"),
                      HUB_CONFIG_DIR=os.path.join(state, "config"), HUB_CLI_WAIT="0", HUB_LANG="en", HUB_NO_MODEL="1")
    for k in ("HUB_EXTENSION", "HUB_MODULE", "HUB_SCHEDULE_JOB"):
        os.environ.pop(k, None)
    loader = importlib.machinery.SourceFileLoader("hub_cliprobe", HUB)
    hub = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_cliprobe", loader))
    loader.exec_module(hub)
    return hub


def stop(*a, **k):
    raise Took()


def probe(hub, st, argv, me):
    """(verdict, text) of one command line: ok | help | form (refused: the words do not fit the command) | refused (by
    the state or who asks) | crash."""
    hub.ME, hub.ARGV = me, []
    hub.REJECTED.clear()
    hub.REJECT_WHY.clear()
    asked, form = [], []
    real_usage, real_value, real_positive = hub.usage, hub.value_of, hub.positive
    real_misuse = getattr(hub, "misuse", None)   # a tree before it: the refusals are the help
    hub.usage = lambda words=None: asked.append(1) or real_usage(words)
    if real_misuse:
        hub.misuse = lambda words=None: form.append(1) or real_misuse(words)

    def value_of(words, flag):
        if flag in words and words.index(flag) + 1 >= len(words):
            form.append(1)
        return real_value(words, flag)

    def positive(text, flag, kind=float):
        try:
            return real_positive(text, flag, kind)
        except SystemExit:
            form.append(1)
            raise
    real_flags = hub.unknown_flags

    def unknown_flags(cmd, rest):
        try:
            return real_flags(cmd, rest)
        except SystemExit:
            form.append(1)
            raise
    hub.value_of, hub.positive, hub.unknown_flags = value_of, positive, unknown_flags
    from core import clihelp
    real_advice = getattr(clihelp.Help, "advice", None)
    if real_advice:
        clihelp.Help.advice = lambda self, *a, **k: form.append(1) or real_advice(self, *a, **k)
    hub.put = stop
    hub.current = lambda: copy.deepcopy(st)
    out, err = io.StringIO(), io.StringIO()
    from core import spool
    patches = [(subprocess, "run"), (subprocess, "Popen"), (subprocess, "check_output"), (subprocess, "call"),
               (os, "execv"), (os, "execvp"), (os, "execvpe"), (urllib.request, "urlopen"), (spool, "put")]
    saved = [(o, n, getattr(o, n)) for o, n in patches]
    for o, n in patches:
        setattr(o, n, stop)
    from modules.critic import critic
    saved.append((critic, "review", critic.review))
    critic.review = stop
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            hub.main(list(argv))
        verdict = "help" if asked else "ok"
    except Took:
        verdict = "ok"
    except SystemExit as e:
        if e.code in (None, 0):
            verdict = "help" if asked else "ok"
        else:   # "form": the words do not fit the command (the refusals that name the form); "refused": the state, who
            verdict = "form" if form or asked else "refused"   # the help instead of the action: the words did not fit
            if isinstance(e.code, str):
                err.write(e.code)
    except Exception as e:  # noqa: BLE001 — a crash is a verdict too
        verdict = "crash"
        err.write(f"{type(e).__name__}: {e}")
    finally:
        for o, n, v in saved:
            setattr(o, n, v)
        hub.usage, hub.value_of, hub.positive, hub.unknown_flags = real_usage, real_value, real_positive, real_flags
        if real_misuse:
            hub.misuse = real_misuse
        if real_advice:
            clihelp.Help.advice = real_advice
    return verdict, (err.getvalue() + out.getvalue()).strip()[:2000]


def main():
    jobs = json.load(sys.stdin)   # [{"argv": [...], "me": "notes"}, …]
    st, _ = fleet()   # first: the tests package sets the environment of its own when imported
    state = tempfile.mkdtemp(prefix="cliprobe-")
    os.makedirs(os.path.join(state, "config"))
    with open(os.path.join(state, "hub.json"), "w") as f:
        json.dump({"critic": {"enabled": True}}, f)
    hub = load(state)
    json.dump([probe(hub, st, j["argv"], j.get("me")) for j in jobs], sys.stdout)


def run(jobs, timeout=600):
    """The verdicts of jobs, from a process of its own (bin/hub sets its process up at import)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
    r = subprocess.run([sys.executable, os.path.abspath(__file__)], input=json.dumps(jobs), capture_output=True,
                       text=True, encoding="utf-8", env=env, cwd=REPO, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr[-2000:])
    return json.loads(r.stdout)


def names():
    """The fixture's names, for the texts' placeholders (the same fleet as main builds)."""
    return fleet()[1]


if __name__ == "__main__":
    sys.path.insert(0, REPO)
    main()
