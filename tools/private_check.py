#!/usr/bin/env python3
"""Test of the private modules against a release candidate (module extraction spec §2.2): a core change can silently break a module
that reads the journal outside the Module API contract (statistics) — publishing, both nightly and a tag, does not go without this test.

    tools/private_check.py <candidate snapshot folder>

The pin is modules/private.json of the candidate itself: the modules repository, a commit, the list of modules. The repository
is cloned at the pinned commit, and its tools/test.py runs the modules' tests against the snapshot (HUB_CORE_REPO, ref WORK).
No pin — nothing to check (0). Refusal with code 8: red tests, no access to the repository, no pinned
commit, a pinned module that was not run, any skipped test. The owner publishes and has access; "skipped"
here would mean a release without a check: on a core lacking what a module needs its tests are skipped, and nothing is red. Without the core
(stdlib): the release test copies only tools/."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

REFUSED = 8
COMMIT = re.compile(r"[0-9a-f]{40}")
# the shape of a retiring pin as hubs read it (modules/update/private.py SLUG, parse; core/modules.py NAME)
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
SUBDIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
CAPS = ("profile", "speech", "stats")


def runner_forms():
    """The pairs of words in the modules runner's line "WORK (<sha>) · <module>: OK|FAILED — <ran> N, <skipped> N": the English
    one, and the earlier forms a pinned commit may still print (language data, `runner.lines`: the pin's own output is not ours
    to change). Read with the stdlib alone, from the repository this tool belongs to: it checks snapshots of any tag."""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "i18n", "data")
    forms = [("tests", "skipped")]
    for name in sorted(os.listdir(root)):
        if name.endswith(".json"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                data = json.load(f)
            for pair in (data.get("runner") or {}).get("lines", []) if isinstance(data, dict) else []:
                if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(w, str) and w for w in pair)):
                    raise ValueError(f"{name}: runner.lines wants pairs of words, got {pair!r}")
                forms.append(tuple(pair))
    return tuple(forms)


FORMS = runner_forms()
LINE = re.compile(r"^WORK \(.*?\) · (\S+): (OK|FAILED) — (?:" + "|".join(re.escape(a) for a, _ in FORMS) + r") (\d+), (?:"
                  + "|".join(re.escape(b) for _, b in FORMS) + r") (\d+)", re.M)
GIT_WAIT = 300
TEST_WAIT = 1800


class Refused(Exception):
    pass


def pin(core):
    """The candidate's pin, or None — there is no file."""
    import glob   # the core's own, else an embed's (core/embed.py: embed/<name>/modules/private.json, by name)
    found = sorted(p for p in glob.glob(os.path.join(glob.escape(core), "embed", "*", "modules", "private.json"))
                   if os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(p)), "embed.json"))
                   and re.fullmatch(r"[a-z0-9_-]{1,32}", os.path.basename(os.path.dirname(os.path.dirname(p)))))
    path = os.path.join(core, "modules", "private.json")
    path = path if os.path.exists(path) or not found else found[0]
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"the candidate's modules/private.json cannot be read ({e}) — the hub would install nothing; not releasing without the check")
    url, commit, mods = (raw.get("url"), raw.get("commit"), raw.get("modules")) if isinstance(raw, dict) else (None,) * 3
    if mods == {} and isinstance(raw, dict) and "retired" in raw:
        # the modules came back into the core: the same shape hubs read (modules/update/private.py parse_retired), or a
        # release that retires nothing on them
        got = raw.get("retired")
        if not (isinstance(raw.get("repo"), str) and SLUG.fullmatch(raw["repo"]) and isinstance(url, str) and url
                and isinstance(got, dict) and got
                and all(isinstance(n, str) and NAME.fullmatch(n) and isinstance(m, dict) and m.get("cap") in CAPS
                        and isinstance(m.get("subdir"), str) and SUBDIR.fullmatch(m["subdir"]) for n, m in got.items())):
            raise Refused("the candidate's modules/private.json retires the modules but hubs cannot read it: repo "
                          "(owner/name), url and retired: {name: {subdir, cap: profile|speech|stats}}")
        return {"retired": sorted(got)}
    if not (isinstance(url, str) and url and isinstance(commit, str) and COMMIT.fullmatch(commit)
            and isinstance(mods, dict) and mods and all(isinstance(m, str) for m in mods)):
        raise Refused("the candidate's modules/private.json needs url, commit (40 characters) and modules — the pin is broken")
    return {"repo": raw.get("repo") or url, "url": url, "commit": commit, "modules": list(mods)}


def clean_env():
    """The caller's environment without git pointers to a foreign repository (GIT_DIR etc.: clone and checkout go into their own folder)."""
    return {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                         "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE")}


def git(*args, cwd=None):
    env = {**clean_env(), "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}   # no login prompt from the background
    try:
        return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_WAIT)
    except subprocess.TimeoutExpired:
        raise Refused(f"git {args[0]} did not answer within {GIT_WAIT} s — no access to the modules repository or the network")


def clone(p, into):
    r = git("clone", "-q", "--no-checkout", "--", p["url"], into)
    if r.returncode:
        raise Refused(f"no access to the modules repository {p['repo']} from this machine: {r.stderr.strip()[-300:]}\n"
                      "the owner publishes and has access: check the login (gh auth status, ssh key) and the GitHub invitation")
    r = git("checkout", "-q", "--detach", p["commit"], cwd=into)
    if r.returncode:
        raise Refused(f"the pinned commit {p['commit'][:7]} is not in the modules repository {p['repo']} — "
                      f"the pin in modules/private.json points nowhere: {r.stderr.strip()[-200:]}")


def run(runner, work, env):
    """(code, output) of the modules' tests; if they do not finish in time — refusal, and the run's processes (it has its own children) are killed
    as a group: the folder with the core and the clone is removed right after."""
    p = subprocess.Popen([sys.executable, runner, "--ref", "WORK"], cwd=work, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=os.name != "nt")
    try:
        out, _ = p.communicate(timeout=TEST_WAIT)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
        except OSError:   # the group has already exited
            pass
        try:
            p.communicate(timeout=10)   # a descendant that left the group holds the pipe — we do not wait further
        except subprocess.TimeoutExpired:
            pass
        raise Refused(f"the private modules' tests did not finish within {TEST_WAIT} s")
    return p.returncode, out


def check(core):
    """Run the modules against the core folder. Returns (result text, code); code 0 — go ahead."""
    tmp = tempfile.mkdtemp(prefix="hub-private-check-")
    try:
        p = pin(core)
        if p is None:
            return "no private modules in this snapshot (modules/private.json) — nothing to check", 0
        if "retired" in p:
            return ("the private modules are retired in this snapshot (modules/private.json: "
                    + ", ".join(p["retired"]) + " — the built-in code) — nothing to check"), 0
        work = os.path.join(tmp, "modules")
        clone(p, work)
        runner = os.path.join(work, "tools", "test.py")
        if not os.path.exists(runner):
            raise Refused(f"the modules repository at {p['commit'][:7]} has no tools/test.py — nothing to check with")
        env = {**clean_env(), "HUB_CORE_REPO": os.path.abspath(core), "PYTHONDONTWRITEBYTECODE": "1"}
        code, out = run(runner, work, env)
        found = {m.group(1): m.groups()[1:] for m in LINE.finditer(out)}
        # a skipped test is an unchecked one: a core lacking what is needed (the test is skipped as "a newer version is needed") would pass silently
        bad = [m for m in p["modules"] if m not in found or found[m][0] != "OK" or int(found[m][1]) == 0
               or int(found[m][2])]
        if code or bad:
            raise Refused(f"the private modules' tests ({p['repo']} {p['commit'][:7]}) did not pass against this snapshot"
                          + (f"; red, skipped or not run: {', '.join(bad)}" if bad else "")
                          + f" — a core edit breaks the module (or the pin is out of date):\n{out[-4000:]}")
        rows = ", ".join(f"{m} {found[m][1]} tests" for m in p["modules"])
        return f"private modules ({p['commit'][:7]}) are green against the snapshot: {rows}", 0
    except Refused as e:
        return str(e), REFUSED
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: tools/private_check.py <candidate snapshot folder>")
    text, code = check(sys.argv[1])
    print(text, file=sys.stderr if code else sys.stdout)
    sys.exit(code)
