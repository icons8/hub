"""A company's build profile (Embed API, spec d2144426 §2.5): which embeds its build carries, how its packages are given
out, where reports of failed updates and issues go. A JSON file; no key or secret in it — only names of files in the
builder's folder of secrets (the hub's settings folder, ~/.config/hub-core/ on Linux, or a path of its own):

    {"profile": 1, "name": "example", "flavor": "team",
     "embeds": [{"name": "example", "repo": "git@git.example.test:hub/embed.git", "ref": "v3"}],
     "deliver": {"url": "https://hub.example.test", "key_file": "deliver-build.key", "report_file": "report-dsn.txt"},
     "issues": "example/hub-issues", "github": "example/hub", "platforms": ["linux-x64"], "forbid_file": ".open-deny"}

- flavor: how the packages are given out — team (by key from a delivery server) or open (open pages and releases);
- embeds[].repo: a git address (a closed one by the builder's own access) or "." — the folder embed/<name>/ already in
  the tree the factory builds; ref: a tag or a commit (not for "."); tools/package/compose.py puts the others in;
- deliver.url is the delivery server (the plan's address too) — or url_file, a file of secrets with it, for a profile
  kept in a tree that may not name the server; key_file its build key, report_file the DSN failed installs and updates
  report to; github — the open build's repository; forbid_file — the forbidden strings the guard
  holds the open flavor to (the flavor open needs it; null — consciously none, the guard checks only the paths).
The factory's flags still name each of these and win over the profile. The stdlib only."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import platforms  # noqa: E402
from core import appcode, embed  # noqa: E402

FORMAT = 1
from layer import ISSUES  # noqa: E402 — ACCOUNT/REPO, one rule with the layer's --issues
PLATFORMS = ("mac-arm64", "mac-x64", "linux-x64", "linux-arm64", "windows-x64", "windows-arm64")


class Bad(Exception):
    """The profile is not one: what is wrong, for a person."""


def load(path):
    """The profile at path, checked. -> dict. Bad — what is wrong."""
    try:
        with open(path, encoding="utf-8") as f:
            prof = json.load(f)
    except (OSError, ValueError) as e:
        raise Bad(f"{path}: not readable ({e})") from None
    return checked(prof, os.path.dirname(os.path.abspath(path)))


def checked(prof, where="."):
    """A profile's fields checked; where — the folder relative paths in it (forbid_file) start from."""
    if not isinstance(prof, dict) or prof.get("profile") != FORMAT:
        raise Bad(f'not a build profile: {{"profile": {FORMAT}, …}}')
    if not isinstance(prof.get("name"), str) or not embed.NAME.match(prof["name"]):
        raise Bad("name: [a-z0-9_-], at most 32")
    if prof.get("flavor", "team") not in appcode.FLAVORS:
        raise Bad(f"flavor: one of {', '.join(appcode.FLAVORS)}")
    embeds = prof.get("embeds", [])
    if not isinstance(embeds, list):
        raise Bad("embeds: a list")
    names = []
    for e in embeds:
        if not (isinstance(e, dict) and isinstance(e.get("name"), str) and embed.NAME.match(e["name"])
                and isinstance(e.get("repo"), str) and e["repo"]):
            raise Bad(f"embeds: {{name, repo, ref}} each, the name [a-z0-9_-] — {e!r}")
        if e["repo"] != "." and not (isinstance(e.get("ref"), str) and e["ref"]):
            raise Bad(f"embeds: {e['name']} from {e['repo']} needs a ref (a tag or a commit)")
        if e["name"] in names:
            raise Bad(f"embeds: {e['name']} twice")
        names.append(e["name"])
    deliver = prof.get("deliver")
    if deliver is not None and not (isinstance(deliver, dict) and isinstance(deliver.get("key_file"), str)
                                    and isinstance(deliver.get("url") or deliver.get("url_file"), str)
                                    and all(isinstance(deliver.get(k), str) for k in ("url", "url_file", "report_file")
                                            if deliver.get(k) is not None)):
        raise Bad("deliver: {url | url_file, key_file, report_file?} — strings")
    if deliver is not None and prof.get("flavor", "team") == "open":
        raise Bad("deliver: the open flavor has no delivery server (its pages are open)")
    if prof.get("issues") is not None and not (isinstance(prof["issues"], str) and ISSUES.match(prof["issues"])):
        raise Bad("issues: ACCOUNT/REPO")
    if prof.get("github") is not None and not (isinstance(prof["github"], str) and ISSUES.match(prof["github"])):
        raise Bad("github: ACCOUNT/REPO")
    plats = prof.get("platforms")
    if plats is not None and not (isinstance(plats, list) and plats and all(p in PLATFORMS for p in plats)):
        raise Bad(f"platforms: some of {', '.join(PLATFORMS)}")
    if prof.get("flavor", "team") == "open" and "forbid_file" not in prof:
        raise Bad("forbid_file: the open flavor's forbidden strings (a file, or null for none)")
    if prof.get("forbid_file") is not None and not isinstance(prof["forbid_file"], str):
        raise Bad("forbid_file: a file or null")
    return {**prof, "flavor": prof.get("flavor", "team"), "embeds": embeds, "_where": where}


def secret(name, secrets=None):
    """The text of a file of secrets: a name in the builder's folder of them, or a path of its own."""
    path = os.path.expanduser(name if os.path.isabs(os.path.expanduser(name)) else
                              os.path.join(secrets or os.environ.get("HUB_CONFIG_DIR") or platforms.current.config_dir(),
                                           name))
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        raise Bad(f"{path}: not readable ({e.strerror})") from None


def deliver(prof, secrets=None):
    """The delivery the environments carry (build.py deliver: {url, key, report?}), or None."""
    d = prof.get("deliver")
    if not d:
        return None
    out = {"url": d.get("url") or secret(d["url_file"], secrets), "key": secret(d["key_file"], secrets)}
    if d.get("report_file"):
        out["report"] = secret(d["report_file"], secrets)
    return out


def server(prof, secrets=None):
    """The delivery server the plan names, or None."""
    d = prof.get("deliver") or {}
    return d.get("url") or (secret(d["url_file"], secrets) if d.get("url_file") else None)


def forbid(prof):
    """The open flavor's forbidden strings of the profile: [] for null, None — the profile says nothing (the
    factory's own rule then)."""
    if "forbid_file" not in prof:
        return None
    if prof["forbid_file"] is None:
        return []
    path = prof["forbid_file"]
    path = path if os.path.isabs(path) else os.path.join(prof.get("_where") or ".", path)
    try:
        with open(path, encoding="utf-8") as f:
            return [s.strip() for s in f if s.strip() and not s.strip().startswith("#")]
    except OSError as e:
        raise Bad(f"forbid_file {path}: not readable ({e.strerror})") from None


def wanted(prof):
    """The names of the embeds the profile's tree carries."""
    return [e["name"] for e in prof["embeds"]]
