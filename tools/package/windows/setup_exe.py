"""Setup.exe of the Windows app (installer spec S6) from a windows-x64 build of tools/package/build.py — on Windows (the
winstand stand): Inno Setup is a Windows program.

  python setup_exe.py <agents-hub-vX.Y.Z-windows-x64.tar.gz | its folder> --out <dir> [--cache <dir>]
        [--sign auto|off|require]
        → <out>/agents-hub-vX.Y.Z-windows-x64-setup.exe (named for the tag: tools/package/upload.py attaches it)

The last step signs it (tools/package/sign.py) where this machine has the signing settings; where it has none (any
machine but the one that makes the team's releases) it says so in one line and the installer stays unsigned, as
before. --sign require — no settings is an error; off — not even looked for. The release machine signs a Setup.exe
made elsewhere with sign.py itself.

Inno Setup comes from its pin in tools/package/versions.json ("inno-setup", sha256 and size checked) and is installed
once for this user into the cache, silently. The script is agents-hub.iss next to this file. The stdlib only."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import fetch  # noqa: E402
import sign  # noqa: E402

ISS = os.path.join(HERE, "agents-hub.iss")
NAME = re.compile(r"^agents-hub-(v\d+\.\d+\.\d+)-windows-x64$")


class Refused(Exception):
    pass


def inno(cache, versions=None, run=subprocess.run):
    """ISCC.exe of the pinned Inno Setup, installed into the cache once."""
    part = next(p for p in fetch.parts(versions or fetch.load(), "windows-x64", tools=True) if p["key"] == "inno-setup")
    place = os.path.join(cache, f"inno-setup-{part['sha256'][:12]}")
    iscc = os.path.join(place, "ISCC.exe")
    if not os.path.exists(iscc):
        try:
            exe = fetch.fetch(part, cache)
        except fetch.Mismatch as e:
            raise Refused(str(e)) from None
        r = run([exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/NOICONS", f"/DIR={place}"],
                capture_output=True, text=True, timeout=600)
        if r.returncode or not os.path.exists(iscc):
            raise Refused(f"Inno Setup is not installed (exit code {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    return iscc


def folder_of(source, work):
    """The app folder: the build's folder as is, or its archive unpacked into work."""
    if os.path.isdir(source):
        return os.path.abspath(source)
    with tarfile.open(source) as t:
        t.extractall(work, filter="data")
    tops = [n for n in os.listdir(work) if os.path.isdir(os.path.join(work, n))]
    if len(tops) != 1:
        raise Refused(f"{source}: the archive does not hold exactly one folder ({tops})")
    return os.path.join(work, tops[0])


def command(iscc, top, out):
    """ISCC's arguments for the app folder top: the tag from the folder's name, the version from its PACKAGED."""
    m = NAME.match(os.path.basename(top))
    if not m:
        raise Refused(f"{os.path.basename(top)} is not a windows-x64 build (agents-hub-vX.Y.Z-windows-x64)")
    with open(os.path.join(top, "PACKAGED")) as f:
        mark = json.load(f)
    if mark.get("platform") != "windows-x64" or f"v{mark.get('version')}" != m.group(1):
        raise Refused(f"PACKAGED is not of {m.group(1)} windows-x64: {mark}")
    for need in ("app.py", "app.ico", "launch.py", "code-seed", os.path.join("runtime", "python", "pythonw.exe")):
        if not os.path.exists(os.path.join(top, need)):
            raise Refused(f"the build has no {need}")
    name = f"{os.path.basename(top)}-setup"
    return [iscc, "/Q", f"/DSource={top}", f"/DVersion={mark['version']}", f"/DOutputDir={os.path.abspath(out)}",
            f"/DOutputName={name}", ISS], os.path.join(out, name + ".exe")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="setup_exe.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("source")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "hub-package"))
    ap.add_argument("--sign", default="auto", choices=("auto", "off", "require"))
    a = ap.parse_args(argv)
    work = tempfile.mkdtemp(prefix="hub-setup-", dir=os.path.dirname(os.path.abspath(a.out)) or None)
    try:
        iscc = inno(a.cache)
        top = folder_of(a.source, work)
        os.makedirs(a.out, exist_ok=True)
        cmd, exe = command(iscc, top, a.out)
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode:
            raise Refused(f"ISCC: exit code {r.returncode}: {(r.stderr + r.stdout).strip()[-1500:]}")
    except Refused as e:
        sys.exit(f"setup_exe.py: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if a.sign == "off":
        print("setup_exe.py: signing is off (--sign off): the installer stays unsigned")
    else:
        try:
            sign.step(exe, require=a.sign == "require")
        except sign.Stop as e:
            sys.exit(f"setup_exe.py: {e}")
    print(f"{exe}  {os.path.getsize(exe)} bytes  sha256 {fetch.sha256(exe)}")


if __name__ == "__main__":
    main()
