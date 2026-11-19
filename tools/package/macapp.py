"""The Mac shell (installer spec S5): "Agents Hub.app" and its .dmg from a mac-arm64 platform folder of build.py.

  python3 tools/package/macapp.py <platform folder> --out <dir> [--cache DIR] [--no-dmg]

On a Mac only (the Mac bridge): clang, codesign, sips, iconutil, hdiutil. Result in <out>:

  Agents Hub.app/                     Contents/MacOS/Agents Hub — deploy/mac/launcher.c, arm64
                                      Contents/Resources/hub — the platform folder + appshell.py (frozen) + bin/git
                                      Contents/Resources/logo.icns, Contents/Info.plist (version of the tag, macOS 13+)
  agents-hub-<tag>-mac-arm64.dmg      the app, a link to /Applications and the instructions as the window's picture
  agents-hub-<tag>-mac-arm64.dmg.json the sheet: sizes, sha256, what was signed

No signing certificate (owner 2026-11-01: the app is unsigned): every Mach-O without a Developer ID gets an ad-hoc
signature, then the app itself (S0-6: `codesign --verify --deep --strict` then holds; node, gh and Git Credential Manager
keep their Developer ID). Python's own modules get bytecode in unchecked-hash mode before signing: a missing .pyc would be
written into the app at the first run and break its seal. Links leaving the app stop the build (codesign --strict
refuses them). The .dmg is made by dmgbuild (Finder's window layout without a Finder — the Mac bridge has no screen),
its wheels pinned by sha256 below. Not a zip: the system `unzip` breaks the signature (S0-6). The stdlib only."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
MAC = os.path.join(REPO, "deploy", "mac")
sys.path.insert(0, HERE)
import fetch  # noqa: E402
from build import Refused, run, tree  # noqa: E402 — the factory's own: one way to run a step and to count a folder

NAME = "Agents Hub"
MIN_MACOS = "13.0"   # Claude Code's own minimum (spec §13 scenario 1); the launcher is built for it too
WINDOW = (720, 480)  # tools/package/mac/background.html is drawn for it
ICONS = {NAME + ".app": (180, 140), "Applications": (540, 140)}
DMGBUILD = [   # dmgbuild and what it imports; pure Python, MIT
    {"key": "dmgbuild", "name": "dmgbuild-1.6.7-py3-none-any.whl", "size": 34853,
     "sha256": "37ee5771c377beb3203d9164aae8046ffed8531c06edf9227f5788b3c599b1bf",
     "url": "https://files.pythonhosted.org/packages/6c/4a/8812638bba991a55a4b670806ab9cf60207077401893bb308eb1b04f288c/"
            "dmgbuild-1.6.7-py3-none-any.whl"},
    {"key": "ds_store", "name": "ds_store-1.3.3-py3-none-any.whl", "size": 16292,
     "sha256": "b92a371efbf1b4ccce2a04d1ed13fceacc4736c81ba09cf5aefb74c088160a35",
     "url": "https://files.pythonhosted.org/packages/66/c0/718a4132306ff8ff052f2c40595f45595993b480409400d55b07f3842d45/"
            "ds_store-1.3.3-py3-none-any.whl"},
    {"key": "mac_alias", "name": "mac_alias-2.2.3-py3-none-any.whl", "size": 21127,
     "sha256": "7362b521d2132ef92f606a37abfed5fcd849ceb2f28b6f9743e014b02af92f0d",
     "url": "https://files.pythonhosted.org/packages/13/20/103f4e4a6e8a524ea92eee7d938c6133d0b129aa999a50f8648ad126c728/"
            "mac_alias-2.2.3-py3-none-any.whl"},
]
# dugite's git is built with prefix "/": without these it looks for its helpers (https!) and templates in /libexec
GIT = """#!/bin/sh
# git of the app (tools/package/macapp.py): dugite's git finds its helpers and templates only through these
G=$(cd "$(dirname "$0")/../runtime/git" && pwd)
GIT_EXEC_PATH="$G/libexec/git-core" GIT_TEMPLATE_DIR="$G/share/git-core/templates" exec "$G/bin/git" "$@"
"""
MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbf"}


def macho(path):
    """A Mach-O file (thin or fat); a Java class shares the fat magic — its next word is a version, not a count."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    if head[:4] in MAGIC:
        return True
    return head[:4] == b"\xca\xfe\xba\xbe" and 0 < int.from_bytes(head[4:8], "big") < 30


def machos(root):
    return sorted(os.path.join(d, n) for d, _, names in os.walk(root) for n in names
                  if not os.path.islink(os.path.join(d, n)) and macho(os.path.join(d, n)))


def outside(root):
    """Links that leave root or are absolute — codesign --strict does not take them."""
    real = os.path.realpath(root)
    bad = []
    for d, dirs, names in os.walk(root):
        for n in dirs + names:
            p = os.path.join(d, n)
            if os.path.islink(p):
                target = os.readlink(p)
                if os.path.isabs(target) or os.path.commonpath([real, os.path.realpath(p)]) != real:
                    bad.append(os.path.relpath(p, root))
    return bad


def developer_id(path):
    """Signed by a Developer ID (node, gh, Git Credential Manager): their signature stays."""
    r = subprocess.run(["codesign", "-dv", "--verbose=2", path], capture_output=True, text=True)
    return r.returncode == 0 and "Authority=Developer ID Application" in r.stderr


def version_of(top):
    with open(os.path.join(top, "PACKAGED")) as f:
        mark = json.load(f)
    if mark.get("format") != "app" or not mark.get("version") or mark.get("platform") != "mac-arm64":
        raise Refused(f"{top}: not the mac-arm64 app folder of build.py (PACKAGED {mark})")
    return mark["version"]


def app(top, out):
    """Agents Hub.app in out from the platform folder top. -> (path, what was signed)."""
    version = version_of(top)
    for need in ("code-seed", "launch.py", "appcode.py", "paths.json", "runtime/python/bin/python3"):
        if not os.path.exists(os.path.join(top, need)):
            raise Refused(f"{top}: no {need}")
    path = os.path.join(out, NAME + ".app")
    shutil.rmtree(path, ignore_errors=True)
    contents = os.path.join(path, "Contents")
    hub = os.path.join(contents, "Resources", "hub")
    os.makedirs(os.path.join(contents, "MacOS"))
    shutil.copytree(top, hub, symlinks=True)
    shutil.copy2(os.path.join(MAC, "appshell.py"), os.path.join(hub, "appshell.py"))
    os.makedirs(os.path.join(hub, "bin"))
    with open(os.path.join(hub, "bin", "git"), "w") as f:
        f.write(GIT)
    os.chmod(os.path.join(hub, "bin", "git"), 0o755)
    bad = outside(path)
    if bad:
        raise Refused(f"links leading outside (codesign --strict does not take them): {', '.join(bad[:5])}")
    python = os.path.join(hub, "runtime", "python", "bin", "python3")
    lib = run([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('stdlib'))"]).strip()
    # Python writes a missing or stale .pyc next to the module: into the app, breaking its seal — unchecked-hash
    # bytecode is never rewritten, whatever the files' times after a copy
    for where in (lib, *(os.path.join(hub, n) for n in ("launch.py", "appcode.py", "shellswap.py")
                         if os.path.exists(os.path.join(hub, n)))):   # appshell.py is the script: never read from bytecode
        # Python's own tests are never imported, and some are broken on purpose (compileall would fail on them)
        run([python, "-I", "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash", "-x",
             "^" + re.escape(lib.rstrip("/")) + "/(test|idlelib/idle_test)/", where], timeout=1800)
    run(["clang", "-Os", "-arch", "arm64", f"-mmacosx-version-min={MIN_MACOS}", "-o",
         os.path.join(contents, "MacOS", NAME), os.path.join(MAC, "launcher.c")])
    mac_app = _deploy_app()
    mac_app.make_icon(os.path.join(REPO, "modules", "web", "favicon.svg"),
                      os.path.join(contents, "Resources", mac_app.ICON + ".icns"))
    import plistlib
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump(mac_app.info_plist(version, MIN_MACOS, mac_app.APP_ID), f)
    signed, kept = [], []
    for f in machos(hub):
        if developer_id(f):
            kept.append(os.path.relpath(f, hub))
            continue
        run(["codesign", "--force", "--sign", "-", f])
        signed.append(os.path.relpath(f, hub))
    run(["codesign", "--force", "--sign", "-", path])
    run(["codesign", "--verify", "--deep", "--strict", path])
    return path, {"ad-hoc": signed, "developer-id": kept}


def _deploy_app():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mac_deploy_app", os.path.join(MAC, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def settings(path, icns):
    """dmgbuild's settings: the app and the Applications link over the instructions picture (with its @2x)."""
    w, h = WINDOW
    return {"files": [path], "symlinks": {"Applications": "/Applications"}, "icon": icns,
            "background": os.path.join(HERE, "mac", "background.png"), "window_rect": ((200, 120), (w, h)),
            "icon_size": 112, "text_size": 13, "icon_locations": ICONS, "format": "UDZO",
            "show_status_bar": False, "show_toolbar": False, "show_pathbar": False, "show_sidebar": False}


def dmg(path, target, cache):
    """The .dmg of the app; it is opened again and the app copied out of it keeps its signature."""
    wheels = [fetch.fetch(w, os.path.join(cache, "dmgbuild")) for w in DMGBUILD]
    sys.path[:0] = wheels
    import dmgbuild
    icns = os.path.join(path, "Contents", "Resources", "logo.icns")
    if os.path.exists(target):
        os.unlink(target)
    dmgbuild.build_dmg(target, NAME, settings=settings(path, icns))
    mount = tempfile.mkdtemp(prefix="hub-dmg-")
    try:
        run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-mountpoint", mount, target])
        try:
            copy = tempfile.mkdtemp(prefix="hub-dmg-copy-")
            try:
                run(["ditto", os.path.join(mount, NAME + ".app"), os.path.join(copy, NAME + ".app")])
                run(["codesign", "--verify", "--deep", "--strict", os.path.join(copy, NAME + ".app")])
            finally:
                shutil.rmtree(copy, ignore_errors=True)
        finally:
            run(["hdiutil", "detach", mount])
    finally:
        shutil.rmtree(mount, ignore_errors=True)
    return target


def build(top, out, cache=None, make_dmg=True):
    if sys.platform != "darwin":
        raise Refused("the Mac app is built only on a Mac (the Mac bridge): codesign, hdiutil, clang")
    cache = cache or os.path.join(os.path.expanduser("~"), ".cache", "hub-package")
    os.makedirs(out, exist_ok=True)
    path, signed = app(os.path.abspath(top), out)
    disk, files = tree(path)
    sheet = {"app": {"disk": disk, "files": files}, "signed": signed}
    if make_dmg:
        name = os.path.basename(os.path.abspath(top).rstrip("/")) + ".dmg"
        target = dmg(path, os.path.join(out, name), cache)
        sheet["dmg"] = {"name": name, "size": os.path.getsize(target), "sha256": fetch.sha256(target)}
        with open(target + ".json", "w") as f:
            json.dump(sheet, f, ensure_ascii=False, indent=1)
    return sheet


def main(argv=None):
    ap = argparse.ArgumentParser(prog="macapp.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("top")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache")
    ap.add_argument("--no-dmg", action="store_true")
    a = ap.parse_args(argv)
    try:
        sheet = build(a.top, a.out, a.cache, not a.no_dmg)
    except Refused as e:
        sys.exit(f"macapp.py: {e}")
    print(f"app: {sheet['app']['disk'] / 1e6:.1f} MB, {sheet['app']['files']} files; signed ad-hoc "
          f"{len(sheet['signed']['ad-hoc'])}, Developer ID kept on {len(sheet['signed']['developer-id'])}")
    if sheet.get("dmg"):
        print(f"dmg: {sheet['dmg']['name']} {sheet['dmg']['size'] / 1e6:.1f} MB sha256 {sheet['dmg']['sha256']}")


if __name__ == "__main__":
    main()
