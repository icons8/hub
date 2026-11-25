"""What of the hub's code folder is not the release (owner 03.11: "tell the hub's own modules from edits of the core
reliably"): one function for the red band in Settings (modules/update) and for the update self-check (tools/test.py),
so the two never disagree. stdlib only and no imports of the hub: tools/test.py runs it on a copy under test.

The working copy is compared with the tree of a base commit (the release installed, the snapshot that was published):
- an edit of the core: a path the base has that is changed or deleted, or a new file inside a folder that the base has
  and that is not the folder of an own module (a new core/x.py, tests/test_x.py, modules/web/static/x.js, a new
  file of a module the release has);
- an own module: a whole new folder modules/<name>/ that the base has no such folder for and that has its manifest
  (module.json), and its bin/hub-<name>; the hub's own code, the release does not depend on it;
- not edits at all: what .gitignore hides, plugins and hub-local (they live outside this folder).
The tree is compared, not the history: the updater's merge of a release and a commit that was taken back differ in
nothing. git alone, no network."""
import os
import re
import subprocess

MANIFEST = "module.json"   # core/modules.py: the file that makes a folder of modules/ a module

# What an edit of the core touches, by its path (dev-guard spec §5.3): the page names an edit in a zone in plain words,
# apart from the rest. A path that ends with "/" is a folder; the first zone that matches wins; any other path is the
# plain core ("core"). By path, not by meaning: a comment fixed in a guard is in the zone too. A big file whose edits are
# mostly features stays plain core even if a check lives in it (modules/web/server.py, hosts/codex.py): its guard parts
# are files of their own where they can be (hosts/codex_rules.py, hosts/codex_sandbox.py).
ZONES = (
    ("guards", ("hooks/", "hosts/common.py", "hosts/nested/", "hosts/codex_rules.py",
                "hosts/codex_sandbox.py", "hosts/codex_liaison.py", "core/guards.py", "core/admit.py", "core/tools.py", "core/perms.py",
                "core/redact.py", "core/plugrights.py", "core/marketplace.py", "core/marketplaces.py", "core/mpinstall.py",
                "core/pgpsig.py", "core/fetch.py", "core/wheels.py", "core/claudeinstall.py", "core/ownclaude.py", "core/codexinstall.py", "core/realclaude.py", "core/configwatch.py",
                "core/specmode.py", "core/hubrun.py", "core/runjobs.py", "core/deputies.py", "core/deputyroute.py",
                "platforms/runwrap.py", "modules/secrets/", "modules/web/secretsweb.py")),
    # ^ what a session may run or reach, who may answer for the owner, which plugins and programs are trusted, which
    # settings a session changed behind the owner's back, the vault of secrets and what hands its values out
    ("access", ("modules/tailnet/", "modules/cloudflare/", "modules/files/server.py", "core/remote.py", "core/jwt.py",
                "core/pagekey.py", "core/pagepass.py", "modules/web/pagekeyweb.py", "modules/web/signin.py",
                "bin/hub-tailnet", "bin/hub-cloudflare", "bin/hub-files")),   # who reaches the page and the files
    ("data_out", ("modules/stats/", "modules/gateway/", "modules/conduit/", "modules/accounts/", "modules/bus_relay/",
                  "core/net.py", "core/nethttp.py", "core/liaison.py", "core/chats.py", "core/chatscli.py",
                  "core/bus.py", "core/busd.py", "core/busrelay.py", "core/cryptolib.py",
                  "core/busstate.py", "core/ghauth.py", "core/mcplogin.py", "core/profiles.py", "core/codexpool.py", "core/codexslot.py",
                  "core/providers.py", "core/accountprobe.py", "core/netprobe.py", "core/cutout.py", "core/embed.py", "hosts/codex_mcp.py",
                  "core/supportbundle.py", "core/hubexport.py", "bin/hub-part", "bin/hub-gateway", "bin/hub-conduit", "bin/hub-stats",
                  "bin/hub-bus-relay")),
    # ^ data that leaves the hub, sign-ins, the hub bus
    ("update", ("tools/test.py", "modules/update/", "bin/hub-update", "core/codeedits.py", "core/codestore.py",
                "core/appcode.py", "core/appwatch.py", "core/shellnext.py", "core/envs.py", "core/appmove.py",
                "core/testlock.py", "core/testsel.py", "core/testtmp.py", "core/ownsrc.py", "modules/testchrome/")),
    # ^ what checks the edits, and the working copy of the core they are made in (a hub from the one-line install)
    ("journal", ("core/journal.py", "core/reducer.py", "core/machines.py", "core/daemon.py", "core/corewrite.py",
                 "core/scrub.py", "core/pathmap.py")),   # the record of facts and the state folded from it
    ("prompts", ("plugins/", "hosts/streamjson.py", "core/minihub.py", "core/modprompt.py", "core/execprofile.py",
                 "core/profcap.py", "hub-home/")),   # what every session (or every executor) reads
    ("tests", ("tests/",)),   # a test of the release edited or deleted, or what the tests read (fakes, replays, helpers)
)
OWN_TEST = re.compile(r"tests/test_[^/]+\.py")   # a new top-level test module: the hub's own, it replaces none of the release
EMBEDDED = re.compile(r"embed/[a-z0-9_-]{1,32}/(.+)")   # a file of an embed (core/embed.py): its path in the core's layout
ZONE_KEYS = tuple(key for key, _ in ZONES)
# the backup branch a take-off of the edits writes (modules/update/takeoff.py): the name the page may be shown
BACKUP_NAME = re.compile(r"backup/local-edits-\d{4}-\d{2}-\d{2}(?:-\d{1,3})?")


def zone(path, st=None):
    """The zone of a path of the code folder (ZONES), "core" when none. With the git letter of the edit (classify's st):
    a new top-level test module (A, or ? outside git) is the hub's own test, not the release's — plain core; a new file
    the release's tests read (tests/fakes/, tests/replays/, a helper) stays in "tests". A file of an embed
    (embed/<name>/<path>, core/embed.py) is in the zone of the same path in the core."""
    m = EMBEDDED.match(path)   # core/embed.core_path — this file imports nothing of the hub
    if m:
        path = m.group(1)
    for key, paths in ZONES:
        if any(path.startswith(p) if p.endswith("/") else path == p for p in paths):
            if key == "tests" and st in ("A", "?") and OWN_TEST.fullmatch(path):
                return "core"
            return key
    return "core"


def _git(repo, *args):
    r = subprocess.run(["git", "--no-optional-locks", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if r.returncode:
        raise OSError(r.stderr.strip()[-200:])
    return r.stdout


def classify(repo, base):
    """{"core": [{"path", "st", "zone"?}], "modules": [name]} sorted, st: M changed, A added, D deleted, ? a new file
    outside git, zone — ZONES, absent for the plain core; None — no answer (not git, no git, a base this copy does not
    have): which is not "no edits"."""
    try:
        tracked = _git(repo, "diff", "--name-status", "--no-renames", "-z", base).split("\0")
        loose = _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        folders = {d for d in _git(repo, "ls-tree", "-r", "-d", "--name-only", "-z", base).split("\0") if d}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    files = {path: st[:1] for st, path in zip(tracked[0::2], tracked[1::2]) if path}
    files.update({path: "?" for path in loose if path})
    own = set()
    for path in files:
        top = path.split("/")
        if len(top) >= 3 and top[0] == "modules" and f"modules/{top[1]}" not in folders \
                and os.path.isfile(os.path.join(repo, "modules", top[1], MANIFEST)):
            own.add(top[1])

    def belongs(path):   # to an own module: its folder or its bin/hub-<name>
        parts = path.split("/")
        return (len(parts) >= 3 and parts[0] == "modules" and parts[1] in own
                or len(parts) == 2 and parts[0] == "bin" and parts[1].startswith("hub-") and parts[1][4:] in own
                and files[path] != "M")
    def entry(path):
        where = zone(path, files[path])
        return {"path": path, "st": files[path], **({"zone": where} if where != "core" else {})}
    return {"core": [entry(p) for p in sorted(files) if not belongs(p)], "modules": sorted(own)}


def release_base(repo, tags, branch=None):
    """The published snapshot the copy is built on — a commit (the branch) or a ref name (a tag), or None (not git, nothing
    published is in HEAD's history). `branch` (a full ref, the one the updater follows): its newest commit that is inside
    HEAD, the merge-base, the rule of the updater's first run (modules/update _target): a release merged by hand counts
    however far the branch went on after it. No such ref (a copy without remote-tracking refs) or no history shared with
    it: the highest release tag vX.Y.Z under the prefix `tags` that is inside HEAD (v1.0.0-rc1 is not a release). The
    updater merges the release into the hub's code, so it is inside HEAD, and what HEAD has beyond it is the hub's own:
    the same base for the red band in Settings (modules/update) and for the update self-check (tools/test.py), whoever
    merged the release and whatever the pass aims at."""
    try:
        if branch:
            try:
                _git(repo, "rev-parse", "-q", "--verify", f"{branch}^{{commit}}")
            except OSError:
                branch = None   # no such ref: the tags
            else:
                try:
                    return _git(repo, "merge-base", "HEAD", branch).strip() or None
                except OSError:
                    pass   # no history shared with the branch (another remote, a shallow copy): the tags, as before
        found = []
        for line in _git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(*objectname)", tags).splitlines():
            name, *commits = line.split()
            tag = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", name[len(tags):])
            if not tag:
                continue
            try:
                _git(repo, "merge-base", "--is-ancestor", commits[-1], "HEAD")
            except OSError:
                continue   # not inside HEAD
            found.append((tuple(map(int, tag.groups())), name))
        return max(found)[1] if found else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
