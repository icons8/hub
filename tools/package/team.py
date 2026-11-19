"""A team's release from the open core and its embed (spec d2597953 §2.3): the open core's tag and the embed's own
repository at the release's tag composed into one tree (compose.py), tagged with the release's version and built by the
factory with the team's profile — the same build a company outside makes of the open core (Embed API).

  python3 tools/package/team.py <vX.Y.Z> --core <open core repository> --embed <the embed's repository>
        --out DIR [--core-ref vA.B.C] [--profile FILE] [-- <factory.py flags: --cache, --mac-parts, --platforms, …>]

- the profile (default: the embed's profile.json in this tree) names its embed with repo "." — the folder of the
  working repository; DIR/profile.json is its copy naming --embed at the tag (the tag goes into no file in git), the
  rest of it as it is (delivery, issues, platforms: names of files in the builder's folder of secrets);
- DIR/tree: compose.py of --core at --core-ref (default: the release's tag; an older one for a release of the embed
  alone) with that embed, tagged with the release's tag. The tree's commit is its own; the layer's MANIFEST "core"
  names the open core's address and commit — what a hub's working copy clones (core/ownsrc.py) — so --core and --embed
  are addresses (https, ssh), never a folder of the builder's: the MANIFEST goes to every hub;
- DIR/build: factory.py <tag> --repo DIR/tree --profile DIR/profile.json and the flags after "--": the layer and the
  environments, checked, and the upload plan; nothing is sent (tools/package/upload.py, on the owner's word).
Both the open core's tag and the embed's must be published first: the layer names their commits. Codes: 0 — built,
1 — refused (the reason is printed). The stdlib only."""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import buildprofile  # noqa: E402
import compose  # noqa: E402
import factory  # noqa: E402
import layer  # noqa: E402

RELEASE = re.compile(r"v\d+\.\d+\.\d+\Z")
PROFILE = os.path.join(ROOT, "embed", "icons8", "profile.json")


class Refused(Exception):
    """The release is not built: what is wrong, for a person."""


# --core: the open core every hub's working copy may clone — https with no sign-in in it (layer.public_url's rule);
# --embed: read by the builder only — https with no sign-in in it, or ssh (git's credentials and keys give the access)
SSH = re.compile(r"ssh://[^\s/:@]+@[^\s/]+/\S+|[\w.-]+@[\w.-]+:[^\s/]\S*")
# the factory's flags team.py sets itself: "--" may not move them, abbreviated either (argparse takes a prefix)
OWN_FLAGS = ("--repo", "--profile", "--out", "--flavor")


def own_flag(arg):
    name = arg.split("=", 1)[0]
    return name.startswith("--") and len(name) > 2 and any(f.startswith(name) for f in OWN_FLAGS)


def profile_for(path, repo, ref, out):
    """The profile at path, checked as the factory checks it, with its one embed of the working repository (repo ".")
    named at repo and ref, written to out -> the copy. A profile with no such embed, or more than one, is refused: which
    one is --embed? A relative forbid_file stays the same file (its folder is the profile's)."""
    try:
        prof = buildprofile.load(path)
    except buildprofile.Bad as e:
        raise Refused(str(e)) from None
    here = [e for e in prof["embeds"] if e["repo"] == "."]
    if len(here) != 1:
        raise Refused(f"{path}: {len(here)} embeds of the working repository (repo \".\"), one is expected")
    here[0].update(repo=repo, ref=ref)
    copy = {k: v for k, v in prof.items() if k != "_where"}
    if isinstance(copy.get("forbid_file"), str) and not os.path.isabs(copy["forbid_file"]):
        copy["forbid_file"] = os.path.join(prof.get("_where") or ".", copy["forbid_file"])
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(copy, f, ensure_ascii=False, indent=1)
    return prof


def tree(prof, core, core_ref, tag, out):
    """compose.py's tree in out, tagged with tag -> the contents of its EMBED.json."""
    made = compose.compose(prof, core, core_ref, out)
    compose.git(out, "tag", tag)
    return made


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    rest = argv[argv.index("--") + 1:] if "--" in argv else []
    ap = argparse.ArgumentParser(prog="team.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("tag", help="the release's version tag vX.Y.Z (the embed's tag; the open core's too by default)")
    ap.add_argument("--core", required=True, help="the open core's repository: https, no sign-in in it")
    ap.add_argument("--core-ref", help="the open core's tag or commit (default: the release's tag)")
    ap.add_argument("--embed", required=True, help="the embed's repository: https with no sign-in in it, or ssh")
    ap.add_argument("--profile", default=PROFILE)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    made_out = False
    try:
        if not RELEASE.match(a.tag):
            raise Refused(f"{a.tag}: a release tag vX.Y.Z")
        if not layer.public_url(a.core):
            raise Refused(f"--core {a.core}: an https address with no sign-in in it — every hub's MANIFEST names it")
        if not (layer.public_url(a.embed) or SSH.fullmatch(a.embed)):
            raise Refused(f"--embed {a.embed}: https with no sign-in in it, or ssh — never a folder of this machine or "
                          "a token in the address")
        moved = [f for f in rest if own_flag(f)]
        if moved:
            raise Refused(f"after --: {', '.join(moved)} — team.py names them to the factory itself")
        if os.path.exists(a.out):
            raise Refused(f"{a.out} exists: the release is built in a new folder")
        os.makedirs(a.out)
        made_out = True
        prof = profile_for(a.profile, a.embed, a.tag, os.path.join(a.out, "profile.json"))
        made = tree(prof, a.core, a.core_ref or a.tag, a.tag, os.path.join(a.out, "tree"))
        print(f"core {made['core']['ref']} {made['core']['commit'][:12]}, "
              + ", ".join(f"{e['name']} {e.get('ref')} {e.get('commit', '')[:12]}" for e in made["embeds"]))
        factory.main([a.tag, "--out", os.path.join(a.out, "build"), "--repo", os.path.join(a.out, "tree"),
                      "--profile", os.path.join(a.out, "profile.json"), *rest])
    except BaseException as e:   # a refusal, the factory's own, a timeout, Ctrl-C: nothing of this run stays
        if made_out:   # the tree's git objects are read-only: Windows deletes them only made writable
            try:
                compose.appcode.remove_tree(a.out)
            except OSError:
                pass
        if isinstance(e, (Refused, compose.Refused, OSError, ValueError)):
            sys.exit(f"team.py: {e}")
        raise


if __name__ == "__main__":
    main()
