"""Anthropic's signed Claude Code releases, without `curl | bash` (installer spec §8): the manifest of a version and
this machine's entry in it, for core/ownclaude, which puts the release's version into the hub's own folder (spec
d2263524 — the hub no longer runs `claude install`: the person's ~/.local/bin is not touched).

1. `<root>/claude-code-releases/<channel>` -> a version (tools/compat.py: the stable channel publish.sh pins);
2. `…/<version>/manifest.json` and its detached signature `manifest.json.sig` — checked against Anthropic's release key
   `<root>/keys/claude-code.asc`, trusted only by the fingerprint pinned here (core/pgpsig, no gpg needed); the
   official install.sh checks only sha256, a manifest without a valid signature is refused here;
3. the platform's entry: the file's name, sha256 and size (core/ownclaude downloads it with core/fetch).
Steps go to `<state>/logs/setup.log`.

Tests point HUB_CLAUDE_DOWNLOADS at a fake server; the fingerprint is not configurable from the environment, so a
mirror or a fake server never makes an unsigned manifest pass."""
import json
import os
import re
import sys
import time

import platforms
from core import fetch, i18n, pgpsig

ROOT = "https://downloads.claude.ai"
ROOT_ENV = "HUB_CLAUDE_DOWNLOADS"
RELEASES = "/claude-code-releases"
KEY_PATH = "/keys/claude-code.asc"
# Anthropic's «Claude Code Release Signing <security@anthropic.com>» key: code.claude.com/docs/en/setup, «Verify the
# manifest signature» (checked 2026-11-01, the same fingerprint in the apt and dnf instructions)
FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
# the words of Anthropic's install.sh/install.ps1 when the version pointer is not a version (an HTML page, a refusal)
REGION = ("Failed to get a valid version from downloads.claude.ai (got unexpected content). This can happen if the "
          "download service is unreachable or not available in your region - see "
          "https://www.anthropic.com/supported-countries")
# a record of what this downloader was last compared with: the official install.sh and install.ps1 (sha256 of
# `curl -fsSL https://claude.ai/install.sh` / install.ps1 that day) and the Claude Code of then. CLAUDE_TESTED moves with
# every release (publish.sh pins the stable channel's version into the snapshot), so the review is due when those
# scripts change: compare the steps above with them (platform choice, manifest fields) and update the record (the
# hashes are not checked by the tests: no network)
REVIEWED = {"claude": "2.1.218", "date": "2026-11-01",
            "install.sh": "3a68d3406cf674e17bed1733a4dcf37805e2e47d87417700007d7e1aa766a944",
            "install.ps1": "cd17c6b555f761d60373659824bf805e1510538226e4c7028e19d7494937a333"}
CHANNELS = ("stable", "latest")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")
BINARY = re.compile(r"claude(?:\.exe)?")
TRIES = fetch.TRIES
AGAIN = "hub setup claude"
# why a step failed (Failed.why): platform|region|network|http|signature|manifest|checksum|broken|disk|version|busy
Failed = fetch.Failed


def root():
    return (os.environ.get(ROOT_ENV) or ROOT).rstrip("/")


class Installer:
    """The steps of one fetch: `say(text)` — the steps for a person, `progress(done, total)` — bytes of the download,
    `pause` — seconds between attempts, `platform` — Anthropic's name of this machine (tests)."""

    def __init__(self, state_dir, root_url=None, fingerprint=FINGERPRINT, say=None, progress=None, pause=2.0,
                 platform=None):
        self.state_dir = state_dir
        self.root = (root_url or root()).rstrip("/")
        self.fingerprint = fingerprint
        self.say = say or (lambda text: None)
        self.progress = progress or (lambda done, total: None)
        self.pause = pause
        self.platform = platform

    # --- the log --------------------------------------------------------------------------------------------
    def log(self, text):
        try:
            os.makedirs(os.path.join(self.state_dir, "logs"), exist_ok=True)
            with open(os.path.join(self.state_dir, "logs", "setup.log"), "a", encoding="utf-8") as f:
                body = "\n    ".join(x for x in str(text).splitlines() if x.strip())   # program output — indented
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} claude: {body}\n")
        except OSError:
            pass   # the log helps support; a read-only state folder must not stop the install

    def tell(self, text):
        self.log(text)
        self.say(text)

    def fail(self, why, text):
        self.log(f"FAILED {why}: {text}")
        return Failed(why, text)

    def _small(self, url):
        try:
            return fetch.small(url)
        except Failed as e:
            raise self.fail(e.why, str(e)) from e

    def _need_ok(self, url):
        status, body = self._small(url)
        if status != 200:
            raise self.fail("http", i18n.t("cli.fetch.http", url=url, status=status))
        return body

    # --- the steps ------------------------------------------------------------------------------------------
    def resolve(self, channel):
        """A channel (stable, latest) -> its version; a version is returned as is."""
        if channel not in CHANNELS:
            return channel
        url = f"{self.root}{RELEASES}/{channel}"
        status, body = self._small(url)
        text = body.decode("utf-8", "replace").strip()
        if status in (403, 451) or (status == 200 and not VERSION.fullmatch(text)):
            raise self.fail("region", i18n.t("cli.claude_setup.region", text=REGION))
        if status != 200:
            raise self.fail("http", i18n.t("cli.fetch.http", url=url, status=status))
        return text

    def manifest(self, ver):
        """The release manifest, only after its signature checked out by the pinned key."""
        base = f"{self.root}{RELEASES}/{ver}"
        raw = self._need_ok(f"{base}/manifest.json")
        status, sig = self._small(f"{base}/manifest.json.sig")
        if status != 200:
            raise self.fail("signature", i18n.t("cli.claude_setup.signature", version=ver,
                                                reason=f"manifest.json.sig: HTTP {status}"))
        key = self._need_ok(self.root + KEY_PATH)
        try:
            pgpsig.verify(key, sig, raw, self.fingerprint)
        except pgpsig.Invalid as e:
            raise self.fail("signature", i18n.t("cli.claude_setup.signature", version=ver, reason=str(e))) from e
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise self.fail("manifest", i18n.t("cli.claude_setup.manifest", version=ver, reason=str(e))) from e
        if not isinstance(data, dict) or data.get("version") != ver or not isinstance(data.get("platforms"), dict):
            # a signed manifest of another release (an old one put behind the pointer) is not this release
            raise self.fail("manifest", i18n.t("cli.claude_setup.manifest", version=ver,
                                               reason=f"version {data.get('version') if isinstance(data, dict) else '?'}"))
        self.tell(i18n.t("cli.claude_setup.signed", version=ver, fingerprint=self.fingerprint[-16:]))
        return data

    def entry(self, data, ver, plat):
        got = data["platforms"].get(plat)
        if not isinstance(got, dict):
            raise self.fail("manifest", i18n.t("cli.claude_setup.no_platform", version=ver, platform=plat))
        name, sha, size = got.get("binary") or platforms.current.CLAUDE_EXE, got.get("checksum"), got.get("size")
        if not (isinstance(name, str) and BINARY.fullmatch(name) and isinstance(sha, str)
                and re.fullmatch(r"[0-9a-f]{64}", sha) and type(size) is int and size > 0):
            raise self.fail("manifest", i18n.t("cli.claude_setup.manifest", version=ver, reason=f"platforms.{plat}"))
        return name, sha, size


def progress_printer(stream=None):
    stream = stream or sys.stderr
    tty = stream.isatty()
    shown = {"step": -1}

    def show(done, total):
        pct = done * 100 // total if total else 100
        if tty:
            stream.write(f"\r  {done / 1e6:6.1f} / {total / 1e6:.1f} MB  {pct:3d}%")
            if done >= total:
                stream.write("\n")
            stream.flush()
        elif pct // 10 != shown["step"]:
            shown["step"] = pct // 10
            stream.write(f"  {pct}%\n")
            stream.flush()
    return show

