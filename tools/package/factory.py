"""The release of the one-line install (spec §9.4, O7): what one release puts on the delivery server — the code layer
and the environment of each of the six builds — built, checked and listed with the commands that send it. Sending is
publishing: this only builds, checks and prints; tools/package/upload.py sends, on the owner's word.

  python3 tools/package/factory.py <vX.Y.Z> --out DIR [--repo P] [--platforms a,b,…] [--mac-parts DIR] [--cache DIR]
        [--deliver-url URL --deliver-key FILE [--report-dsn FILE]] [--issues ACCOUNT/REPO] [--server URL]
        [--keep-folders] [--flavor open [--github ACCOUNT/REPO]] [--profile FILE]
  python3 tools/package/factory.py mac-parts --out DIR [--cache DIR]      (on a Mac)
  python3 tools/package/factory.py layer <ref> --out DIR [--repo P] [--channel stable|nightly] [--cache DIR]
        [--server URL] [--profile FILE]

A tag (vX.Y.Z):
- each platform of --platforms (all six by default) is built here by build.py, with one Python compiling the code, so
  the six carry the same layer. The one part only a Mac makes — our ffmpeg for mac-arm64 and mac-x64, built from
  source with Apple's tools — comes from `factory.py mac-parts --out DIR` run on a Mac of the same tag, its DIR copied
  here; --mac-parts DIR puts them into this machine's cache, each checked by its sha256, its folder and its programs'
  headers; a part of another versions.json is left out. No key goes to the Mac;
- with --deliver-url, every environment carries the team build's deliver.json (build.py: the address, the build key —
  it only reads releases — and with --report-dsn the Sentry DSN failed installs report to), the same in all six;
- every sheet is checked: its platform and tag, one runtime and one layer (the manifest's sum) for all six, the
  archive's size and sha256 on disk, build.py's check of the programs (each of the platform's OS and processor; the x64
  half of Git for Windows on ARM counted as emulated) — a mismatch stops the release and names it;
- <out>/release.json — what was checked; <out>/upload-plan.txt — the commands, in the order a hub can live with: the
  environments and the scripts first, the layer last (a hub sees a release when its channel names the layer);
- the unpacked platform folders go once checked (--keep-folders keeps them): each is 0.5-0.8 GB.
A nightly needs only its layer (`layer <ref>`): an environment is built again only for a new runtime (versions.json
"runtime"), with the tag that brings it.
--profile FILE — a company's build profile (Embed API, tools/package/buildprofile.py): its flavor, delivery (the
address, the build key, the DSN — files in ~/.config/hub-core/), issues, platforms and open repository instead of the
flags, which still win; its embeds are checked in the tree (core/embed.py: the rules and the contract of the slots,
as for every profile) and named in the layer's MANIFEST. A tree of embeds from other repositories is made first by
tools/package/compose.py.
--flavor open — the open build (agents-hub-<tag>-open-<platform>, its layer held against .open-deny of the main copy):
no deliver.json, no key; it goes to no delivery server — the plan is tools/package/pages.py (the pages at --server, the
environments among the files of the GitHub release of the tag in --github) and upload.py's release of the tag. Codes: 0 — built and checked, 1 — refused (the reason is printed). The stdlib
only."""
import argparse
import json
import os
import shutil
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build  # noqa: E402
import buildprofile  # noqa: E402
import fetch  # noqa: E402
import layer  # noqa: E402

PLATFORMS = ("mac-arm64", "mac-x64", "linux-x64", "linux-arm64", "windows-x64", "windows-arm64")
MAC = ("mac-arm64", "mac-x64")
SERVER = "https://<delivery server>"
GITHUB = "<account>/<repo>"   # the open build's repository and its pages, for its plan
PAGES = "https://<account>.github.io/<repo>"
UPLOAD = "python3 tools/package/upload.py"
PARTS = "mac-parts.json"   # what mac-parts left: {cache name: {platform, part, sha256, size}}


class Refused(Exception):
    """The release is not ready: what is wrong, for a person."""


def name_of(tag, plat, flavor="team"):
    return f"agents-hub-{tag}{'-open' if flavor == 'open' else ''}-{plat}"


def read_sheet(path):
    try:
        with open(path, encoding="utf-8") as f:
            sheet = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"{os.path.basename(path)}: no sheet ({e})") from None
    if not isinstance(sheet, dict):
        raise Refused(f"{os.path.basename(path)}: not a sheet")
    return sheet


def wanted_parts(versions):
    """{cache name: (platform, part)} of what a Mac makes for this versions.json (build.made_here)."""
    return {build.ffmpeg_key(versions[key]["build"][plat]): (plat, key)
            for plat in MAC for key in build.made_here(versions, plat)}


def mac_parts(out, versions=None, cache=None):
    """On a Mac: the parts only it makes, built (or taken from the cache) and packed into out with their sums."""
    versions = versions or fetch.load()
    cache = cache or os.path.expanduser("~/.cache/hub-package")
    os.makedirs(out, exist_ok=True)
    told = {}
    for name, (plat, key) in sorted(wanted_parts(versions).items()):
        scratch = os.path.join(out, name + ".check")
        try:
            build.ffmpeg_mac(versions[key]["build"][plat], cache, scratch)   # builds once, checks the arch every time
        except build.Refused as e:
            raise Refused(f"{plat}: {e}") from None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        path = os.path.join(out, name + ".tar.gz")
        with tarfile.open(path + ".part", "w:gz") as t:
            t.add(os.path.join(cache, name), arcname=name, filter=build._norm)
        os.replace(path + ".part", path)
        told[name] = {"platform": plat, "part": key, "sha256": fetch.sha256(path), "size": os.path.getsize(path)}
        print(f"{plat}: {key} {told[name]['size'] / 1e6:.1f} MB", flush=True)
    with open(os.path.join(out, PARTS), "w", encoding="utf-8") as f:
        json.dump(told, f, indent=1)
    return told


def bring_parts(src, versions, cache):
    """The parts a Mac made into this machine's cache: only those this versions.json wants, each of its sum; the cache
    keeps one already there (the same source and configure line made it). -> the names brought."""
    try:
        with open(os.path.join(src, PARTS), encoding="utf-8") as f:
            told = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"--mac-parts {src}: no {PARTS} ({e})") from None
    brought = []
    for name, (plat, key) in wanted_parts(versions).items():
        entry = told.get(name) if isinstance(told, dict) else None
        if not isinstance(entry, dict):
            continue                           # missing: build.py says how to make it
        path = os.path.join(src, name + ".tar.gz")
        if not os.path.isfile(path) or os.path.getsize(path) != entry.get("size") or fetch.sha256(path) != entry.get("sha256"):
            raise Refused(f"{plat}: {name}.tar.gz is not the part {PARTS} names (size or sha256)")
        place = os.path.join(cache, name)
        if os.path.isdir(place):
            continue
        tmp = place + ".part"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        with tarfile.open(path) as t:
            if any(m.name != name and not m.name.startswith(name + "/") for m in t.getmembers()):
                raise Refused(f"{plat}: {name}.tar.gz holds more than its folder")
            t.extractall(tmp, filter="data")
        os.replace(os.path.join(tmp, name), place)
        shutil.rmtree(tmp, ignore_errors=True)
        brought.append(name)
    return brought


def check(sheets, out, tag):
    """One release: each sheet of its platform and tag, its archive on disk as the sheet says, the programs checked by
    the platform, one runtime and one layer for all. -> {runtime, layer} they share."""
    runtimes, sums = set(), set()
    for plat, sheet in sheets.items():
        if sheet.get("platform") != plat or sheet.get("tag") != tag:
            raise Refused(f"{plat}: the sheet is of {sheet.get('platform')} {sheet.get('tag')}, not {plat} {tag}")
        arc = sheet.get("archive") or {}
        path = os.path.join(out, str(arc.get("name")))
        if not os.path.isfile(path) or os.path.getsize(path) != arc.get("size") or fetch.sha256(path) != arc.get("sha256"):
            raise Refused(f"{plat}: {arc.get('name')} is not the archive its sheet names (size or sha256)")
        form, want = build.ARCH[plat]
        got = sheet.get("arch") or {}
        if (got.get("format"), got.get("want")) != (form, want) or not got.get("programs"):
            raise Refused(f"{plat}: the sheet has no check of its programs as {form} {want} (a build.py before O7?)")
        if sheet.get("skipped"):
            raise Refused(f"{plat}: built without {', '.join(sheet['skipped'])} — a trial, not a release")
        runtimes.add(sheet.get("runtime"))
        sums.add((sheet.get("layer") or {}).get("sum"))
    if len(runtimes) != 1 or len(sums) != 1 or None in sums:
        raise Refused(f"the builds differ: runtime {sorted(map(str, runtimes))}, layer {sorted(map(str, sums))} — "
                      "build them all from one tag and one versions.json")
    return {"runtime": runtimes.pop(), "sum": sums.pop()}


def plan(server, sheets, lay, runtime, channel, out):
    """The commands that send the release, without --send: the environments, the scripts, the layer last."""
    rows = []
    for plat, sheet in sheets.items():
        rows.append(f"{UPLOAD} {server} shell {os.path.join(out, sheet['archive']['name'])} --platform {plat} "
                    f"--runtime {runtime}")
    if sheets:
        for script in ("install.sh", "install.ps1"):
            rows.append(f"{UPLOAD} {server} script deploy/oneline/{script} --version {lay['version']}")
    rows.append(f"{UPLOAD} {server} layer {os.path.join(out, lay['archive']['name'])} --channel {channel} "
                f"--version {lay['version']} --commit {lay['commit']} --runtime-min {lay['runtime_min']}")
    rows.append(f"{UPLOAD} {server} show")
    return rows


def plan_open(server, github, tag, out, relay=None):
    """The commands that send an open release, without --send: the pages' tree and the release's files, then its
    upload to the GitHub release of the tag (the pages go out as one commit of their branch). relay — the file with the
    public relay's line the pages carry (`none` — no relay); not given — a name that is no file, so pages.py refuses
    until the release names one."""
    publish = os.path.join(out, "publish")
    return [f"python3 tools/package/pages.py {out} --out {publish} --base {server} "
            f"--assets https://github.com/{github}/releases/download/{tag} --relay {relay or 'PUBLIC_RELAY_LINE_FILE'}",
            f"{UPLOAD} {github} {tag} {os.path.join(publish, 'release')}{os.sep}*"]


def write(out, summary, rows):
    with open(os.path.join(out, "release.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "upload-plan.txt"), "w", encoding="utf-8") as f:
        f.write("# without --send each only looks; with it — publishing, on the owner's word\n")
        f.write("".join(r + "\n" for r in rows))


def release(tag, out, repo, platforms=PLATFORMS, parts=None, cache=None, deliver=None, issues=None, server=SERVER,
            keep_folders=False, versions=None, python=None, skip=(), flavor="team", forbid=None, github=GITHUB,
            profile=None, embeds=None, relay=None):
    """Build and check one tag's release into out (parts — the folder of a Mac's mac-parts). -> the summary
    (release.json). flavor open: forbid — its forbidden strings (default: .open-deny of the main copy of repo);
    profile, embeds — the company's build profile's name and its embeds (tools/package/buildprofile.py)."""
    if not layer.RELEASE.match(tag or ""):
        raise Refused(f"{tag!r}: a release tag vX.Y.Z (a nightly has only its layer: `layer <ref>`)")
    if flavor not in layer.appcode.FLAVORS:
        raise Refused(f"--flavor {flavor!r}: one of {', '.join(layer.appcode.FLAVORS)}")
    if flavor == "open" and deliver:
        raise Refused("the open build carries no deliver.json: its hubs read its pages without a key")
    if flavor == "open" and server == SERVER:
        server = PAGES
    if flavor == "open" and forbid is None:
        try:
            forbid = layer.deny_of(repo)
        except layer.Refused as e:
            raise Refused(str(e)) from None
    bad = [p for p in platforms if p not in PLATFORMS]
    if bad or not platforms:
        raise Refused(f"--platforms: of {', '.join(PLATFORMS)}")
    os.makedirs(out, exist_ok=True)
    versions = versions or fetch.load()
    cache = cache or os.path.expanduser("~/.cache/hub-package")
    if parts:
        for name in bring_parts(parts, versions, cache):
            print(f"{name}: brought into the cache", flush=True)
    sheets, lay = {}, None
    for plat in platforms:
        try:
            sheet = build.build(plat, out, repo=repo, tag=tag, issues=issues, cache=cache, versions=versions,
                                python=python, skip=skip, deliver=deliver, flavor=flavor, forbid=forbid or (),
                                profile=profile, embeds=embeds, check=plat == platforms[0])   # one commit: once
        except build.Refused as e:
            raise Refused(f"{plat}: {e}") from None
        lay = lay or sheet.get("layer")
        print(f"{plat}: built, {sheet['archive']['size'] / 1e6:.1f} MB", flush=True)
        if not keep_folders:
            shutil.rmtree(os.path.join(out, name_of(tag, plat, flavor)), ignore_errors=True)
        sheets[plat] = sheet
    shared = check(sheets, out, tag)
    summary = {"tag": tag, "flavor": flavor, "runtime": shared["runtime"], "layer": lay, "deliver": bool(deliver),
               "platforms": {p: {"archive": s["archive"], "disk": s["total"]["disk"], "files": s["total"]["files"],
                                 "arch": s["arch"]} for p, s in sheets.items()}}
    rows = plan_open(server, github, tag, out, relay) if flavor == "open" else \
        plan(server, sheets, lay, shared["runtime"], "stable", out)
    write(out, summary, rows)
    return summary


def nightly(ref, out, repo, channel=None, cache=None, server=SERVER, versions=None, python=None, profile=None,
            embeds=None, issues=None):
    """The layer of a commit (a nightly: <the release before>-nightly.<time>.<sha>), its plan of one command."""
    os.makedirs(out, exist_ok=True)
    versions = versions or fetch.load()
    cache = cache or os.path.expanduser("~/.cache/hub-package")
    try:
        lay = layer.build(repo, ref, out, python or build.tool(versions, "python", cache), channel=channel,
                          issues=issues, profile=profile, wanted=embeds, check=True)
    except (layer.Refused, layer.appcode.Bad, build.Refused) as e:
        raise Refused(str(e)) from None
    summary = {"ref": ref, "layer": lay}
    write(out, summary, plan(server, {}, lay, versions["runtime"], lay["channel"], out))
    return summary


def table(summary):
    rows = [("platform", "archive, MB", "on disk, GB", "files", "programs")]
    for plat, p in summary["platforms"].items():
        a = p["arch"]
        emu = f" (+{sum(a['emulated'].values())} of another processor, known)" if a["emulated"] else ""
        rows.append((plat, f"{p['archive']['size'] / 1e6:.1f}", f"{p['disk'] / 1e9:.2f}", str(p["files"]),
                     f"{a['programs']} {a['want']}{emu}"))
    width = [max(len(r[i]) for r in rows) for i in range(5)]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, width)).rstrip() for r in rows)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="factory.py", description=__doc__.split("\n\n")[0])
    nightly_mode = bool(argv) and argv[0] == "layer"
    if argv and argv[0] == "mac-parts":
        ap.add_argument("--out", required=True)
        ap.add_argument("--cache")
        a = ap.parse_args(argv[1:])
        try:
            mac_parts(a.out, cache=a.cache)
        except (Refused, OSError) as e:
            sys.exit(f"factory.py: {e}")
        return
    ap.add_argument("ref", help="the tag vX.Y.Z (or after `layer`: any commit)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(HERE)))
    ap.add_argument("--cache")
    ap.add_argument("--server", help="the delivery server the plan names (default: the profile's deliver.url)")
    ap.add_argument("--profile", help="the company's build profile (tools/package/buildprofile.py): its flavor, "
                                      "embeds, delivery, issues, platforms; the flags below win over it")
    if nightly_mode:
        ap.add_argument("--channel", choices=("stable", "nightly"))
    else:
        ap.add_argument("--platforms")
        ap.add_argument("--mac-parts", help="the parts a Mac made (factory.py mac-parts there)")
        ap.add_argument("--deliver-url")
        ap.add_argument("--deliver-key", help="a file with the build key")
        ap.add_argument("--report-dsn", help="a file with the Sentry DSN of failed installs (with --deliver-url)")
        ap.add_argument("--issues")
        ap.add_argument("--keep-folders", action="store_true")
        ap.add_argument("--flavor", choices=("team", "open"))
        ap.add_argument("--github", help="the open build's repository (its plan: the release of the tag)")
        ap.add_argument("--relay", help="the open build: a file with the public relay's line its pages carry, or none")
    a = ap.parse_args(argv[1:] if nightly_mode else argv)
    try:
        prof = buildprofile.load(a.profile) if a.profile else None
        if prof and not nightly_mode:
            a.flavor = a.flavor or prof["flavor"]
            a.platforms = a.platforms or ",".join(prof.get("platforms") or ())
            a.issues = a.issues or prof.get("issues")
            a.github = a.github or prof.get("github")
        # the profile's delivery is the team flavor's: --flavor open over a team profile takes neither its server nor keys
        team = (a.flavor or "team") == "team" if not nightly_mode else not prof or prof["flavor"] == "team"
        a.server = a.server or (buildprofile.server(prof) if prof and team else None) or SERVER
        named = {"profile": prof["name"], "embeds": buildprofile.wanted(prof)} if prof else {}
        if nightly_mode and not team:
            raise Refused("an open profile has no nightly here: the open build goes out by its tag (pages and releases)")
        if nightly_mode:
            s = nightly(a.ref, a.out, a.repo, a.channel, a.cache, a.server, **named,
                        issues=prof.get("issues") if prof else None)
            print(f"{s['layer']['archive']['name']}  {s['layer']['channel']}  {s['layer']['archive']['size'] / 1e6:.1f} MB")
        else:
            a.flavor, a.platforms, a.github = a.flavor or "team", a.platforms or ",".join(PLATFORMS), a.github or GITHUB
            deliver = buildprofile.deliver(prof) if prof and team and not (a.deliver_url or a.deliver_key or a.report_dsn) \
                else None
            if a.deliver_url or a.deliver_key or a.report_dsn:
                deliver = {"url": a.deliver_url or ""}
                for key, path in (("key", a.deliver_key), ("report", a.report_dsn)):
                    if path:
                        with open(path, encoding="utf-8") as f:
                            deliver[key] = f.read().strip()
                if not deliver.get("key"):
                    raise Refused("--deliver-url goes with --deliver-key (and --report-dsn with both)")
            forbid = None   # the open flavor: .open-deny of the main copy, plus the profile's own strings
            own = buildprofile.forbid(prof) if prof and a.flavor == "open" else None
            if own is not None:   # a company's checkout has no .open-deny: its profile's list is its whole list
                forbid = layer.deny_of(a.repo, missing_ok=True) + own
            s = release(a.ref, a.out, a.repo, [p for p in a.platforms.split(",") if p], a.mac_parts, a.cache, deliver,
                        a.issues, a.server, a.keep_folders, flavor=a.flavor, forbid=forbid, github=a.github,
                        relay=a.relay, **named)
            print(table(s))
        print(f"the plan: {os.path.join(a.out, 'upload-plan.txt')}")
    except (Refused, OSError, buildprofile.Bad, layer.Refused) as e:
        sys.exit(f"factory.py: {e}")


if __name__ == "__main__":
    main()
