"""The tree of a company's build (Embed API, spec d2144426 §2.6): the open core of a tag and the embeds of its profile
from their own repositories, one commit the factory builds from.

  python3 tools/package/compose.py --profile P --core <repository> --ref <vX.Y.Z|commit> --out DIR

- the core is the commit as git archives it; each embed of the profile whose repo is not "." comes from its repository
  at its ref (a closed one by the builder's own access: git's credentials, an SSH key) into embed/<name>/, as git
  archives that commit — its own embed.json, profile.json, modules/, i18n/, tests/ (the rest of its repository —
  a README, .gitignore — stays there);
- the rules of embedding (core/embed.py problems: the embed's paths, no file of the core replaced, no path twice, one
  slot one embed, exactly the profile's embeds) hold the tree before anything is written; a broken one is refused;
- EMBED.json at the top says what it is made of: {"profile", "core": {"repo", "ref", "commit"}, "embeds": [{"name",
  "repo", "ref", "commit"}]} — addresses without a sign-in in them; the factory copies it into the layer's MANIFEST;
- DIR becomes a git repository of one commit (it must not exist): the company tags it with its own version and the
  factory builds that tag (tools/package/factory.py --profile P --repo DIR). Embeds with repo "." are already in the core's
  tree. The stdlib only."""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import buildprofile as profiles  # noqa: E402
from core import appcode, embed  # noqa: E402

TOP = "EMBED.json"


class Refused(Exception):
    """The tree is not made: what is wrong, for a person."""


def git(repo, *args, data=False):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=600, stdin=subprocess.DEVNULL,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode:
        raise Refused(f"git {' '.join(args[:2])}: {r.stderr.decode(errors='replace').strip()[-400:]}")
    return r.stdout if data else r.stdout.decode().strip()


def plain(url):
    """An address without a user or a token in it (what EMBED.json may name)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme and u.netloc:
        return urllib.parse.urlunsplit((u.scheme, u.hostname + (f":{u.port}" if u.port else ""), u.path, "", ""))
    return re.sub(r"^[^@/]+@(?=[^:/]+:)", "", url) if "://" not in url and "@" in url.split(":", 1)[0] else url


def unpack(data, into, only=None):
    """A tar archive into a folder; only(name) — which members to take (an embed's repository: what an embed is)."""
    os.makedirs(into, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        members = [m for m in t.getmembers() if only is None or only(m.name)]
        t.extractall(into, members=members, filter="data")


def embedded(name):
    """A file of an embed's repository that the embed carries (core/embed.py: ALLOWED, TOP); its README, .gitignore,
    tools stay in its repository."""
    return name.startswith(embed.ALLOWED) or name in embed.TOP or any(a.startswith(name + "/") for a in embed.ALLOWED)


def fetch(repo, ref, work):
    """(the archive of repo at ref, its commit): a local repository is read in place, any other is cloned bare."""
    if os.path.isdir(repo):
        local = repo
    else:
        local = os.path.join(work, "clone")
        r = subprocess.run(["git", "clone", "-q", "--bare", repo, local], capture_output=True, timeout=900,
                           stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode:
            raise Refused(f"{plain(repo)}: not cloned ({r.stderr.decode(errors='replace').strip()[-300:]})")
    commit = git(local, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return git(local, "archive", "--format=tar", commit, data=True), commit


def compose(prof, core, ref, out):
    """The tree of prof (profile.load) on the core repository at ref, in out (must not exist), committed. -> the
    contents of EMBED.json."""
    if os.path.exists(out):
        raise Refused(f"{out} exists: the tree is made in a new folder")
    work = tempfile.mkdtemp(prefix="hub-compose-")
    try:
        data, commit = fetch(core, ref, os.path.join(work, "core"))
        unpack(data, out)
        here = {e["name"] for e in prof["embeds"] if e["repo"] == "."}   # the embeds the core's tree already has
        top = os.path.join(out, embed.FOLDER)
        for name in sorted(os.listdir(top)) if os.path.isdir(top) else []:
            if name not in here:   # another company's, or one this profile does not carry
                appcode.remove_tree(os.path.join(top, name))
        made = {"profile": prof["name"], "core": {"repo": plain(core), "ref": ref, "commit": commit}, "embeds": []}
        for e in prof["embeds"]:
            if e["repo"] == ".":
                made["embeds"].append({"name": e["name"], "repo": "."})
                continue
            os.makedirs(os.path.join(work, e["name"]))
            data, got = fetch(e["repo"], e["ref"], os.path.join(work, e["name"]))
            unpack(data, os.path.join(out, embed.FOLDER, e["name"]), embedded)
            made["embeds"].append({"name": e["name"], "repo": plain(e["repo"]), "ref": e["ref"], "commit": got})
        bad = embed.problems(out, wanted=profiles.wanted(prof))
        if bad:
            raise Refused("the embeds break the rules (core/embed.py):\n" + "\n".join(bad[:20]))
        with open(os.path.join(out, TOP), "w", encoding="utf-8", newline="\n") as f:
            json.dump(made, f, ensure_ascii=False, indent=1)
        for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                     ("-c", "user.name=factory", "-c", "user.email=factory@example.test", "commit", "-q", "--no-verify",
                      "-m", f"{prof['name']}: core {ref} with {', '.join(profiles.wanted(prof)) or 'no embeds'}")):
            git(out, *args)
        return made
    except BaseException:
        if os.path.isdir(out):
            appcode.remove_tree(out)
        raise
    finally:
        appcode.remove_tree(work)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="compose.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", required=True)
    ap.add_argument("--core", required=True, help="the open core's repository: a path or an address")
    ap.add_argument("--ref", required=True, help="its tag vX.Y.Z or a commit")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        made = compose(profiles.load(a.profile), a.core, a.ref, a.out)
    except (Refused, profiles.Bad) as e:
        sys.exit(f"compose.py: {e}")
    print(f"{a.out}: core {made['core']['commit'][:12]} with "
          f"{', '.join(e['name'] for e in made['embeds']) or 'no embeds'} — tag it, then factory.py --profile")


if __name__ == "__main__":
    main()
