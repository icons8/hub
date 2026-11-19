""""Agents Hub.app" and the launchd agent for the hub on a Mac (the idea and launcher.c came from a colleague's PR).

launchd starts not python3 but the bundle's executable (launcher.c), and hub-up is its child. macOS attributes the
background work and the folder access to the bundle: "Login Items" shows the hub's name and logo, not "python3"; access
to the repository folder (for example ~/Desktop) is granted once per bundle, the children get the same.

The bundle is built once on a machine with a Developer ID certificate and can lie in the repository ready-made
(deploy/mac/Agents Hub.app): colleagues need no compiler and get no "unidentified developer" mark. The open flavor
carries instead "Hub by Icons8.app", built from the same launcher.c, universal and signed ad hoc (it lies in
deploy/mac/open/, the open cut puts it in place of the team's: core/appcode.OPEN_SWAP). A bundle's name is its folder's:
macOS shows the folder's name, and the executable and Info.plist carry the same one. A checkout without a ready bundle
still installs: install.sh builds the bundle from launcher.c with an ad-hoc signature. The bundle does not
store the paths to Python and hub-up — the launchd agent passes them, so the bundle is the same for everyone and the
signature stays intact.

  app.py build "<Developer ID Application: …>"   build and sign deploy/mac/Agents Hub.app (after editing launcher.c)
  app.py build - [BUNDLE] [--id ID]                ad-hoc signature, no certificate: what install.sh builds when the checkout
                                                   has no ready bundle; BUNDLE — where to put it (its folder's name is
                                                   the app's); ID — another hub's own identifier (HUB_INSTANCE:
                                                   com.hub-<name>); the open flavor's: app.py build -
                                                   "deploy/mac/open/Hub by Icons8.app" (its identifier OPEN_ID)
  app.py name                                      the name of the bundle a hub of this tree keeps in its state folder
  app.py names                                     the names of every flavor's (one a line)
  app.py drop-others STATE PLIST                   the other flavor's copy of an earlier install goes, once the agent
                                                   PLIST runs through this tree's bundle
  app.py app-plist REPO STATE APP                  the downloaded app (installer S5): the agent runs APP's shell
                                                   (deploy/mac/appshell.py) with --serve
  app.py config STATE APP HUB_JSON                 hub.json of a hub from the app, if there is none yet
  app.py plist REPO STATE PYTHON [BUNDLE]          write ~/Library/LaunchAgents/com.hub-core.plist; without BUNDLE —
                                                   launchd starts Python directly (no bundle or the signature is wrong)
"""
import json
import math
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

NAME = "Agents Hub"
LABEL = "com.hub-core"
# the downloaded app's own bundle identifier (installer S5): the bundle of a hub from git — maybe another hub on the same
# Mac — is LABEL with another signature, and macOS keeps folder permissions and Login Items by identifier and signature
APP_ID = "com.hub-core.app"
# the open flavor's bundle (core/appcode.OPEN_SWAP): its own name and identifier — macOS keeps folder access and Login
# Items by identifier and signature, an ad-hoc one is not the team's Developer ID
OPEN_NAME = "Hub by Icons8"
OPEN_ID = "com.icons8.hub"
# the bundles a state folder may keep (install.sh, deploy/oneline/setup.py): the team's and the open flavor's
NAMES = (NAME + ".app", OPEN_NAME + ".app")
ICON = "logo"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def shipped(here=HERE):
    """The bundle that lies ready in the tree (deploy/mac/<name>.app): the team's or the open flavor's, or None."""
    for name in NAMES:
        if os.path.isdir(os.path.join(here, name)):
            return os.path.join(here, name)
    return None


BUNDLE = shipped() or os.path.join(HERE, NAME + ".app")


def name_of(bundle):
    """The app's name: its folder's, without .app (what macOS shows)."""
    base = os.path.basename(os.path.normpath(bundle))
    return base[:-4] if base.endswith(".app") and len(base) > 4 else NAME


def executable(bundle):
    """<bundle>/Contents/MacOS/<its CFBundleExecutable> (the name for a bundle without a readable one)."""
    return os.path.join(bundle, "Contents", "MacOS", _info(bundle).get("CFBundleExecutable") or name_of(bundle))
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/"
              "lsregister")


def oklch_hex(lightness, chroma, hue):
    """A CSS oklch() colour as sRGB #rrggbb (Björn Ottosson's formulas)."""
    a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
              -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
              -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)

    def channel(x):
        x = min(1.0, max(0.0, x))
        return round(255 * (12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055))
    return "#" + "".join(f"{channel(x):02x}" for x in linear)


def logo_svg(svg):
    """favicon.svg for sips: oklch converted to sRGB (sips draws it black), with a margin around the drawing, like macOS icons."""
    svg = re.sub(r"oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)", lambda m: oklch_hex(*map(float, m.groups())), svg)

    def pad(m):
        x, y, w, h = map(float, m.group(1).split())
        side = max(w, h) * 1024 / 824
        return f'viewBox="{x - (side - w) / 2:g} {y - (side - h) / 2:g} {side:g} {side:g}"'
    return re.sub(r'viewBox="([^"]+)"', pad, svg, count=1)


def info_plist(version="1.0", minimum="12.0", identifier=LABEL, name=NAME):
    return {"CFBundleName": name, "CFBundleDisplayName": name, "CFBundleIdentifier": identifier,
            "CFBundleExecutable": name, "CFBundleIconFile": ICON, "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version, "CFBundleVersion": version, "LSMinimumSystemVersion": minimum,
            "LSUIElement": True}


STOP_TIMEOUT = 75   # seconds from SIGTERM to SIGKILL: longer than hub-up waits for the core (platforms/posix.stop_grace, 60 s)


def agent_plist(python, repo, state, bundle=None, label=LABEL, env=None):
    home = os.path.expanduser("~")
    path = [os.path.join(state, "bin"), os.path.join(repo, "bin"), os.path.join(home, ".local", "bin"),
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    log = os.path.join(state, "logs", "hub-up.log")
    run = [python, os.path.join(repo, "bin", "hub-up")]
    # a second hub (HUB_INSTANCE): its own label and variables — the settings folder, ports
    # HUB_SERVICE: `hub stop` refuses and names this command (the owner pastes it into a terminal) — KeepAlive would bring the
    # hub up again
    agent = {"Label": label, "ProgramArguments": run, "WorkingDirectory": repo,
             "EnvironmentVariables": {"HUB_STATE_DIR": state, "PATH": ":".join(path),
                                      "HUB_SERVICE": f"launchctl bootout gui/$(id -u)/{label}",
                                      "HUB_SERVICE_INTERACTIVE": "1", **(env or {})},
             "RunAtLoad": True, "KeepAlive": True, "StandardOutPath": log, "StandardErrorPath": log,
             # stopping: hub-up waits for the core snapshot up to stop_grace, while launchd by default kills the group after about 20 seconds
             "ExitTimeOut": STOP_TIMEOUT,
             # a job without ProcessType is Standard: light resource limits. Under load the core's loop then got a
             # fraction of a core, read the sessions' facts minutes late and waited for processes that had already
             # exited. Interactive = no limits, as an app; HUB_SERVICE_INTERACTIVE tells platforms.below_hub to put
             # sessions, plugins and the parts other than the core's own back below it (a service of an earlier
             # install has no mark: nothing is clamped until the service is installed again)
             "ProcessType": "Interactive"}
    if bundle:
        agent["ProgramArguments"] = [executable(bundle)] + run
        # "Login Items" (macOS 13+) show the agent under the app — with its name and icon from Info.plist;
        # the agent and the bundle are signed by one command (tried: the hub icon shows)
        agent["AssociatedBundleIdentifiers"] = [bundle_id(bundle) or LABEL]
    return agent


def _info(bundle):
    """The bundle's Info.plist as {key: a non-empty string value}, {} when it is missing or unreadable."""
    from xml.parsers.expat import ExpatError
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            got = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError, AttributeError):
        return {}
    return {k: v for k, v in got.items() if isinstance(v, str) and v} if isinstance(got, dict) else {}


def bundle_id(bundle):
    """The identifier in the bundle's Info.plist (a second hub's is com.hub-<name>), or None."""
    return _info(bundle).get("CFBundleIdentifier")


def app_agent_plist(repo, state, app):
    """The downloaded app (installer S5, deploy/mac/appshell.py): launchd runs the app's shell with --serve — it brings
    the code and the environment up and becomes hub-up; the app, not the code, knows where its Python is."""
    agent = agent_plist("python3", repo, state, app)
    agent["ProgramArguments"] = [os.path.join(app, "Contents", "MacOS", NAME), "--serve"]
    # the state folder, not the code: launchd does not start a job whose folder is missing, and the shell is what
    # brings the code back from the seed (it goes into the code itself)
    agent["WorkingDirectory"] = state
    agent["AssociatedBundleIdentifiers"] = [APP_ID]
    env = agent["EnvironmentVariables"]
    env["PATH"] += ":/usr/sbin:/sbin"
    return agent


def app_config(state, app):
    """hub.json of a hub from the app: what the .deb writes (tools/package/first_run.py), with the programs of the
    app's environment named through <state>/app (the shell keeps the link on the app the hub runs from)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("first_run", os.path.join(REPO, "tools", "package", "first_run.py"))
    first_run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(first_run)
    with open(os.path.join(app, "paths.json")) as f:
        paths = json.load(f)
    where = lambda key: os.path.join(app, paths[key]) if paths.get(key) else None   # noqa: E731
    mcp = [where("node"), where("mcp")] if where("mcp") and where("node") else None
    return first_run.config(state, mcp=mcp, ffmpeg=where("ffmpeg"))


def make_icon(svg_path, icns_path):
    with tempfile.TemporaryDirectory() as tmp:
        svg = os.path.join(tmp, "logo.svg")
        with open(svg_path) as src, open(svg, "w") as dst:
            dst.write(logo_svg(src.read()))
        iconset = os.path.join(tmp, "logo.iconset")
        os.mkdir(iconset)
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ""), (2, "@2x")):
                n = str(size * scale)
                subprocess.run(["sips", "-s", "format", "png", "-z", n, n, svg, "--out",
                                os.path.join(iconset, f"icon_{size}x{size}{suffix}.png")],
                               check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns_path], check=True)


def build(bundle, identity="-", identifier=None):
    """Rebuild the bundle: the launcher for Apple Silicon and Intel, the icon, Info.plist, the signature (identity "-" — without a
    certificate, ad hoc). identifier: a second hub's own (macOS keeps Login Items and folder access by it); by default
    the main hub's of the flavor — LABEL, OPEN_ID for the open one's. The app's name is the folder's (name_of): the
    executable and Info.plist carry it. The open flavor's bundle is signed ad hoc only (no company's certificate)."""
    name = name_of(bundle)
    if name == OPEN_NAME and identity != "-":
        raise SystemExit(f"{bundle}: the open flavor's bundle is signed ad hoc only (identity -)")
    identifier = identifier or (OPEN_ID if name == OPEN_NAME else LABEL)
    shutil.rmtree(bundle, ignore_errors=True)
    contents = os.path.join(bundle, "Contents")
    os.makedirs(os.path.join(contents, "MacOS"))
    os.makedirs(os.path.join(contents, "Resources"))
    subprocess.run(["clang", "-Os", "-arch", "arm64", "-arch", "x86_64", "-mmacosx-version-min=12.0",
                    "-o", os.path.join(contents, "MacOS", name), os.path.join(HERE, "launcher.c")], check=True)
    make_icon(os.path.join(REPO, "modules", "web", "favicon.svg"), os.path.join(contents, "Resources", ICON + ".icns"))
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump(info_plist(identifier=identifier, name=name), f)
    sign = ["codesign", "--force", "--sign", identity]
    if identity != "-":
        sign += ["--options", "runtime", "--timestamp"]
    subprocess.run(sign + [bundle], check=True)


def set_file_icon(bundle):
    """The icon on the bundle's executable: "Login Items" take the name from the bundle and the icon from this file (a colleague's
    PR). Git does not store file attributes — it is set at install time, after the signature check: a strict check
    (--strict) does not match with them, the regular one and launching do."""
    icns = os.path.join(bundle, "Contents", "Resources", ICON + ".icns")
    exe = executable(bundle)
    script = ('ObjC.import("AppKit"); $.NSWorkspace.sharedWorkspace.setIconForFileOptions('
              f'$.NSImage.alloc.initWithContentsOfFile({json.dumps(icns)}), {json.dumps(exe)}, 0)')
    out = subprocess.run(["osascript", "-l", "JavaScript", "-e", script], capture_output=True, text=True)
    return out.stdout.strip() == "true"


def drop_others(state, agent, run=subprocess.run):
    """The other flavor's bundle an earlier install left in the state folder (NAMES) goes, out of LaunchServices first
    (Login Items find a registered bundle by its identifier) — only once the agent runs through one of them: a bundle
    that did not copy or verify leaves the earlier one in place. -> [the paths that went]."""
    args = agent.get("ProgramArguments") if isinstance(agent, dict) else None
    first = str(args[0]) if isinstance(args, list) and args else ""
    keep = next((n for n in NAMES if first.startswith(os.path.join(state, n) + os.sep)), None)
    gone = []
    for n in NAMES if keep else ():
        old = os.path.join(state, n)
        if n != keep and os.path.isdir(old) and not os.path.islink(old):
            try:
                run([LSREGISTER, "-u", old], capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):   # no LaunchServices here: the folder goes all the same
                pass
            shutil.rmtree(old, ignore_errors=True)
            gone.append(old)
    return gone


def verified(bundle):
    """The bundle is intact and signed (after git clone and copying)."""
    return os.path.isdir(bundle) and subprocess.run(["codesign", "--verify", "--strict", bundle],
                                                    capture_output=True).returncode == 0


def main(argv):
    identifier = None
    if argv[:1] == ["build"] and argv[-2:-1] == ["--id"]:
        identifier, argv = argv[-1], argv[:-2]
    if argv[:1] == ["build"] and len(argv) in (2, 3):
        bundle = argv[2] if len(argv) == 3 else BUNDLE
        build(bundle, argv[1], identifier)
        print(bundle)
    elif argv == ["name"]:
        print(os.path.basename(BUNDLE))
    elif argv == ["names"]:
        print("\n".join(NAMES))
    elif argv[:1] == ["drop-others"] and len(argv) == 3:
        state, path = argv[1:3]
        with open(path, "rb") as f:
            for gone in drop_others(state, plistlib.load(f)):
                print(gone)
    elif argv[:1] == ["app-plist"] and len(argv) == 4:
        repo, state, app = argv[1:4]
        path = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)   # a fresh account has no LaunchAgents yet
        with open(path, "wb") as f:
            plistlib.dump(app_agent_plist(repo, state, app), f)
        # the app is not touched: no file icon (it would break the seal), LaunchServices knows an app it opened
        print(path)
    elif argv[:1] == ["config"] and len(argv) == 4:
        state, app, path = argv[1:4]
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(app_config(state, app), f, ensure_ascii=False, indent=2)
        print(path)
    elif argv[:1] == ["plist"] and len(argv) in (4, 5):
        repo, state, python = argv[1:4]
        bundle = argv[4] if len(argv) == 5 and verified(argv[4]) else None
        # a second hub: the label and variables come from the installer environment (install.sh, HUB_INSTANCE)
        label = os.environ.get("HUB_LABEL") or LABEL
        env = {k: os.environ[k] for k in ("HUB_CONFIG_DIR", "HUB_WEB_PORT", "HUB_FILES_PORT") if os.environ.get(k)}
        path = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", label + ".plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump(agent_plist(python, repo, state, bundle, label=label, env=env), f)
        if bundle:  # the app goes into LaunchServices so that "Login Items" find it by identifier
            subprocess.run([LSREGISTER, "-f", bundle], capture_output=True)
        if bundle and not set_file_icon(bundle):  # the icon on the file — for macOS without AssociatedBundleIdentifiers
            print("the hub's icon is not set — Login Items will show a generic one", file=sys.stderr)
        print(path)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
