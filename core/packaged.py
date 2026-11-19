"""A hub from a package (.deb, .app): a PACKAGED file next to the code {"version", "format", "issues", ...}.

Two kinds. An older package (format "deb", the code a snapshot without git) has no update from git (owner decision
2026-10-06: there must be no auto-updates) — a new version is installed as a new package; this is not a setting, hub.json
will not enable it. The app (format "app", installer spec S3, variant A): the shell keeps the environment, the code is a
git clone of the shared repo on a release tag in the state folder, and PACKAGED is written into it by the shell's launcher
({"format": "app", "version", "runtime": N, "download": "https://…", "issues"}). It updates through modules/update on the
stable channel, the schedule only looks and the owner installs with a button (the same 2026-10-06 decision); "runtime" is
the number of the environment the shell carries, a release that needs a newer one (core/version.RUNTIME_MIN) waits for a
new app ("download" — where to get it).

The Hub Developer of a packaged hub is different (core/dev.py): it does not edit package code — it writes plugins and
files issues in the public repo from the "issues" field."""
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = os.environ.get("HUB_PACKAGED_MARK") or os.path.join(REPO, "PACKAGED")


def __getattr__(attr):
    """`packaged.HOW` (bin/hub prints it for `hub update` on a package): the text in this process's language, read on use,
    not frozen at the import in the language of that moment."""
    if attr == "HOW":
        from core import i18n
        return i18n.t("core.packaged.how")
    raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")


def info():
    """Contents of PACKAGED, or None (a hub from git)."""
    try:
        with open(MARK) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_app(data):
    return isinstance(data, dict) and data.get("format") == "app"


def app_settings(data):
    """update.app of an app: the environment number and where a newer app is (only an https address: the page makes a
    link of it)."""
    out = {}
    runtime = data.get("runtime")
    if isinstance(runtime, int) and not isinstance(runtime, bool) and runtime >= 0:
        out["runtime"] = runtime
    if data.get("layout") == "layer":   # the code layer (app spec §3.1): releases from the distribution server
        out["layer"] = True
    platform = data.get("platform")   # which new app of releases.json is this shell's (app spec §4.4)
    if isinstance(platform, str) and re.fullmatch(r"[a-z0-9]+-[a-z0-9_]+", platform):
        out["platform"] = platform
    url = data.get("download")
    if isinstance(url, str) and url.startswith("https://") and len(url) <= 300 and not any(c.isspace() for c in url):
        out["download"] = url
    return out


def apply(cfg):
    """Settings of a packaged hub, whatever hub.json says: an older package has no update from git; the app updates from
    the stable channel (update.app — what modules/update does differently for it). A hub from git cannot claim update.app:
    it skips the self-check, which a hub with personal commits needs."""
    data = info()
    up = {k: v for k, v in (cfg.get("update") or {}).items() if k != "app"}
    if data is None:
        if "app" in (cfg.get("update") or {}):
            cfg["update"] = up
        return cfg
    if is_app(data):
        # one channel (the owner's decision of 2026-11-09, m1917882: "yes, only one channel"): every package the
        # distribution server carries is a release; a hub from git follows main (core/version.channel)
        cfg["update"] = {**up, "enabled": True, "channel": "stable", "remote": "origin", "branch": "main",
                         "follow": False,
                         "app": app_settings(data)}
    else:
        cfg["update"] = {**up, "enabled": False}
    return cfg


def how(data):
    """Why the "Updates" setting is locked on this packaged hub (the "Settings" row), in the process language."""
    from core import i18n   # lazily: bin/hub-up loads this file early
    if is_app(data):
        return i18n.t("core.update.layer_how" if data.get("layout") == "layer" else "core.update.app_how")
    return i18n.t("core.packaged.how")


def issues_url(data):
    repo = str((data or {}).get("issues") or "")
    return f"https://github.com/{repo}/issues/new" if repo.count("/") == 1 else None
