"""The app's update by the button, from the distribution server (app spec §4, slice A3; the owner's decision of 2026-11-07: the hub only
looks and shows, the owner installs). The code is a layer (core/appcode.py), not a git clone: an update is a whole new
layer next to the current one, and the pointer <state>/code moves to it at the next full restart.

- Where from: layers/deliver.json {url, key} — the shell's (tools/package/launch.py copies it there; the key reads the
  releases only and never goes into a layer). GET <url>/v1/releases.json with the key: {"channels": {"stable"|"nightly":
  {version, commit, published_at, runtime_min, notes, layer: {url, sha256, size}}}} (deliver/server.py). The open
  build has no key: {url} of its pages (tools/package/pages.py), the same releases.json read without one; its layers
  are on the pages, its environments assets of its GitHub releases (core/fetch.asset_ok: every redirect too), each
  checked by its size and sha256 as the team build's.
- One channel, stable (the owner's decision of 2026-11-09, m1917882): every package on the server is a release; no
  delay. Never down: a layer older than the running one is not offered (a hub that once took a nightly waits for a
  stable newer than itself).
- The install: every pass of the schedule installs in the background (update.plan; the owner's decision of 2026-11-09,
  m1917410) and asks the person for the restart (_restart_or_ask); the owner's own install (`hub update`) and the
  first start restart the hub by themselves. The hub no longer tells the server where it stands (POST /v1/stand of
  A3n): nobody waits on that number now that no one presses "Update".
- The install itself: the archive into layers/archives/code-<name>.tar.gz (core/fetch.py: the key goes only to the
  server's own host, a broken download continues; the archive stays — the shell's launcher takes the released files
  from it to repair an edited layer), checked against releases.json (size, sha256) and unpacked against its MANIFEST
  (appcode.unpack);
  a newer shell (runtime_min) needed — it waits, as the app from git; the layer's own Claude Code (its
  CLAUDE_TESTED) is downloaded into the hub's folder first and the release waits for it (base Updater._claude_up).
  Then layers/next.json for hub-up (core/appcode.take_next), the trial for the previous release's watchdog (trial.py:
  the hub must come up on the new layer, else the pointer goes back and the layer is marked in layers/failed.json),
  update.applied and the restart (_restart_or_ask).
- A layer that did not come up is not offered again ("did not come up, running on Y"): the next newer one is.
- "Return <previous>": the previous layer (layers/history.json) at the next restart, the returned one is not offered
  again by the schedule.
- The first start of an app (layers/fresh.json, the launcher's): the channel's latest layer is installed without a
  button — that is the installation, not an update (§4.3); the record goes when that is done or nothing is newer.
- A release that needs a newer environment (runtime_min above the shell's runtime): the new app of this platform from
  releases.json's runtime.apps (app spec §4.4) is offered with the same button. The press downloads it into
  layers/shells/ (the sum of releases.json) and the shell's own tools/package/shellswap.py checks it and lays it out
  (record layers/shell-update.json, "ready"); the full restart then swaps it (core/shellnext.py, hub-up) under the old
  shell's watchdog, and once the hub is up on it the release is installed by itself (the record's "then"). Linux: the
  .deb is downloaded and the page names the command that installs it (root); a hub that finds a newer app on disk under
  itself asks for the restart that takes it. A new app that did not come up is not offered again.
  The one-line install (its environment a folder, <state>/app the pointer to it — spec §6.2): the new environment from
  releases.json's runtime.shells, downloaded into <data>/downloads, unpacked and checked by the scripts' own program
  (deploy/oneline/unpack.py) into <data>/shells next to the current one, its installer's own steps done; the old
  environment's watchdog moves the pointer at the full restart — the same on the three systems, no root on Linux. The
  environments no hub needs any more go (core/envs.py).
- The person's own build (`hub app apply`, core/ownsrc.py: <release>+local.<sha>, its manifest's "own"): apply_own
  takes it from layers/.own-<sha> and leaves it for the person's restart under the same watchdog, its way back the
  clean release it is over (not a previous build); no report to the developers of a build of the person's (nor of its
  not coming up — the Hub Developer is told why). On it the hub stands on that release: the same release is "current",
  "Return" is to it ("Return the released"), and a newer release installs as always — clean, with one line that the
  edits are off and stay in the working copy.
- Kept on disk: the current layer, the one left for the restart, the previous ones of the history (HISTORY_KEEP), their
  archives and the last copy of an edit; the rest goes (on Windows when no process holds it)."""
import contextlib
import datetime
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse

import platforms
from core import appcode, catalog, news, ownsrc, shellnext
from core.coretext import T
from modules.update import report
from modules.update import update as base

DELIVER = "deliver.json"
HISTORY = "history.json"
FAILED = "failed.json"
FRESH = "fresh.json"
ARCHIVES = "archives"
SHELLS = "shells"           # the downloaded new apps (tools/package/shellswap.py SHELLS)
SHELL_ASKED = "shell-asked.json"   # the restart already asked for a newer app installed under the running hub
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,150}\Z")
CHANNEL = "stable"   # the one channel of a hub on a layer (owner 2026-11-09, m1917882): every package is a release
SHA = re.compile(r"[0-9a-f]{64}\Z")
KEY = re.compile(r"[A-Za-z0-9_-]{16,200}\Z")
SIZE_MAX = 200 * 1024 * 1024   # an archive bigger than this is not a layer (appcode.SIZE_MAX unpacked)
NOTES_MAX = 12
MINIMUM = "min.json"   # layers/min.json: the distribution server's open minimum version, as last read (app spec §7.2)
STOPS = ("claude", "update-format", "bus", "stats")   # what may stop below the minimum: the core names only these
STAGE_KEEP = 24 * 3600   # a build nobody took (the command cut off) goes after a day: one running now is younger


OWN_NOT_UP_DEV = T("core.update.own_not_up_dev",
                   "The hub did not come up on {name}, the build of the working copy (hub app apply), within 3 minutes "
                   "— it is back on the released {previous}. The reason is in the logs ({logs}, hub-up.log first): fix it "
                   "on local with a test, then hub app apply again.")


class Refused(Exception):
    """The server's answer is not what the update reads (a form, a sum): the check result 'error' with this text."""


class Passing(Refused):
    """The server is busy (a 5xx, 408, 429): the result 'error' too, but a state — no report (app spec §7.1)."""


def layers_dir(state):
    return os.path.join(state, "layers")


def source(state, flavor="team"):
    """{url, key} of the distribution server, or None: the shell names none, or what it names is not an https address
    and a key (http only to this machine: a stand). The open flavor's has no key: {url, key: None}."""
    try:
        with open(os.path.join(layers_dir(state), DELIVER), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(got, dict):
        return None
    url, key = str(got.get("url") or "").rstrip("/"), str(got.get("key") or "")
    parts = urllib.parse.urlsplit(url)
    local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
    if not ((parts.scheme == "https" or local) and parts.hostname):
        return None
    if KEY.match(key):
        return {"url": url, "key": key}
    return {"url": url, "key": None} if flavor == "open" and not key else None


def minimum_of(doc, flavor):
    """The flavor's entry of min.json, checked: {version, reason: {language: text}, since?, stops: [known]}, or None."""
    got = doc.get(flavor) if isinstance(doc, dict) else None
    if not (isinstance(got, dict) and isinstance(got.get("version"), str) and appcode.NAME.match(got["version"])):
        return None
    reason = got.get("reason") if isinstance(got.get("reason"), dict) else {}
    out = {"version": got["version"],
           "reason": {k: v[:2000] for k, v in reason.items() if isinstance(k, str) and len(k) <= 16 and isinstance(v, str)},
           "stops": [s for s in got.get("stops") or [] if s in STOPS] if isinstance(got.get("stops"), list) else []}
    if isinstance(got.get("since"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", got["since"]):
        out["since"] = got["since"]
    return out


def release_items(text):
    """The release's notes (CHANGES.md entries, as the tag message has them) as a list: a date heading is the bare date, an
    item is its "- " line with the indented lines that continue it, a long one cut at a word."""
    out, cur = [], None
    for line in text.splitlines():
        if cur is not None and line.strip() and line[:1] in " \t":
            cur.append(line.strip())
            continue
        cur = None
        if line.startswith("## "):
            out.append([line[3:].strip()])
        elif line.strip().lstrip("-*• ").strip():
            cur = [line.strip().lstrip("-*• ").strip()]
            out.append(cur)
    return [news.shorten(" ".join(x), news.ITEM_MAX) for x in out if " ".join(x)]


def entry_of(doc, channel):
    """The channel's release in releases.json, checked: {version, commit, notes: [lines], runtime_min, layer: {url,
    sha256, size}}; None — the channel has none; Refused — a form the update does not read."""
    if not isinstance(doc, dict) or not isinstance(doc.get("channels", {}), dict):
        raise Refused("releases.json: not {channels: {...}}")
    got = doc.get("channels", {}).get(channel)
    if got is None:
        return None
    layer = got.get("layer") if isinstance(got, dict) else None
    version = got.get("version") if isinstance(got, dict) else None
    if not (isinstance(version, str) and appcode.NAME.match(version) and isinstance(layer, dict)
            and isinstance(layer.get("url"), str) and isinstance(layer.get("sha256"), str)
            and SHA.match(layer["sha256"]) and type(layer.get("size")) is int and 0 < layer["size"] <= SIZE_MAX):
        raise Refused(f"releases.json: the {channel} release is not {{version, layer: {{url, sha256, size}}}}")
    runtime = got.get("runtime_min", 0)
    notes = got.get("notes") if isinstance(got.get("notes"), str) else ""
    return {"version": version, "commit": str(got.get("commit") or "")[:64],
            "runtime_min": runtime if type(runtime) is int and runtime >= 0 else 0,
            # the release's lines as CHANGES.md has them ("- …"): the text of each, the page and Telegram draw the bullet
            "notes": release_items(notes)[:NOTES_MAX],
            "layer": layer}


class LayerUpdater(base.Updater):
    """The app's update on a code layer. The same results and facts as the update from git (update.checked, applied,
    failed, owner.notify): "About the hub" and Telegram read them as they are."""

    def __init__(self, repo, state_dir, cfg, claude=base.claude_version, spawn=None, small=None, download=None,
                 now=time.time, run=subprocess.run, reporter=None, post=None, claude_install=base.install_claude):
        super().__init__(repo, state_dir, cfg, claude=claude, spawn=spawn, claude_install=claude_install)
        from core import fetch
        self.small = small or fetch.small
        self.post = post or fetch.post
        self.download = download or fetch.download
        self.fetch_failed = fetch.Failed
        self.now = now
        self.run = run   # the shell's shellswap.py prepare
        self.doc = {}    # releases.json of this pass
        self.reporter = reporter or report.Reporter(state_dir, cfg, now=now)
        self.caught = None   # the exception behind a failed step of this pass: its traceback goes with the report
        self.stage = None    # where an install of this pass is: download | verify | switch (an error's step)

    # --- what is where

    def codes(self):
        return layers_dir(self.state_dir)

    def link(self):
        return os.path.join(self.state_dir, "code")

    def running(self):
        """(name, manifest without its files) of the layer the pointer leads to; (None, {}) — none."""
        name = appcode.current(self.link(), self.codes())
        if not name:
            return None, {}
        try:
            man = appcode.load(os.path.join(self.codes(), name))
        except appcode.Bad:
            return name, {}
        return name, {k: v for k, v in man.items() if k != "files"}

    def _layers_read(self, name, kind):
        try:
            with open(os.path.join(self.codes(), name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return kind()
        return data if isinstance(data, kind) else kind()

    def _layers_write(self, name, data):
        catalog.write_private(os.path.join(self.codes(), name), data)

    def _history(self):
        """The layers that ran before the current one, [{name, version}], newest last."""
        return [h for h in self._layers_read(HISTORY, list) if isinstance(h, dict) and isinstance(h.get("name"), str)
                and appcode.NAME.match(h["name"])]

    def _failed(self):
        return {k: v for k, v in self._layers_read(FAILED, dict).items() if isinstance(k, str)}

    def _next(self):
        note = self._layers_read(appcode.NEXT, dict)
        return note.get("name") if isinstance(note.get("name"), str) else None

    def check(self, apply=True, manual=False, restart=None):
        """As Updater.check; a server answer the update does not read is the result "error" with the app's keys (the
        page keeps "Return" and the app's texts), not an exception out of the pass. restart — an installed release asks
        for the hub's restart itself (the owner's install, the first start); otherwise (a background install) the person
        is asked to restart (_stage). By default — when the owner asks (manual)."""
        self.info, self.apply, self.manual, self.caught, self.stage = {}, apply, manual, None, None
        self.claude_tried = False
        self.restart = manual if restart is None else restart
        passing = False
        try:
            result = self._check()
            self._claude_own(result)   # its claude older than its own release's: installed now
        except Exception as e:  # noqa: BLE001 — a form, a sum, the disk: said on the page, the next pass tries again
            self.info["error"] = (str(e) if isinstance(e, (Refused, appcode.Bad, OSError)) else repr(e))[:300]
            result, self.caught, passing = "error", e, isinstance(e, Passing)
        self._app_info()
        self.put("update.checked", None, {"result": result, **self.info})
        self._report(result, passing)   # after the fact: the page and Telegram do not wait for the network
        return result

    # --- reports to the company's Sentry (modules/update/report.py)

    STEP_OF = {"error": "check", "broken": "download", "bad-archive": "verify", "bad-shell": "shell"}

    def _report(self, result, passing=False):
        """A failed step of this pass to Sentry (once per version and step); what waits from earlier passes goes too.
        An error past the reading of the releases is the step the install was at (self.stage) and the version it
        installed; the reading itself — the running version. A report never stops the update: its own failure is a
        line in the log."""
        try:
            step = self.STEP_OF.get(result)
            if result == "error" and self.stage:
                step = self.stage
            if step and not passing:
                to = self.info.get("version") if step != "check" else None
                to = to or self.running()[1].get("version") or ""   # a build of edits: no report (report.py)
                self.reporter.failed(step, to, self, result, self.info.get("error") or "", self.caught)
            self._restored_report()
        except Exception as e:  # noqa: BLE001
            print(f"update: the report failed: {e!r}", flush=True)
        try:   # its own try: a failure above does not keep it, nor the flush, from the pass
            self._shell_report()
            self.reporter.flush()
        except Exception as e:  # noqa: BLE001
            print(f"update: the report failed: {e!r}", flush=True)

    def _restored_report(self):
        """The shell's launcher put the release back because the core did not start on an edited layer (or could not put
        it back): layers/restored.json — one report per layer version (the owner's own "Return the released" is none)."""
        rec = self._layers_read("restored.json", dict)
        name, man = self.running()
        at = rec.get("at")
        if rec.get("name") != name or not (rec.get("why") == "start" or rec.get("error")) \
                or not isinstance(at, (int, float)) or isinstance(at, bool):
            return
        if self.reporter.marked("restored") == [name, at]:
            return   # this record is told already: the file stays for hubd, the next pass is not a repeat of it
        got = self.reporter.failed("restore", man.get("version") or name, self, "restored", str(rec.get("error") or ""),
                                   extra={"restored": rec})
        if got != "off":   # told (sent, waiting, refused or a repeat): reports turned on later still tell it
            self.reporter.mark("restored", [name, at])

    SHELL_REPORT_AGE = 7 * 86400   # an older record (before this code, written up to the owner long ago) is not news

    def _shell_report(self):
        """A new environment its watchdog took the hub back from: one report per environment (its sha256), at the end of
        the pass like every report (the live probe p62: the person was told, the developers were not). Its own mark, not
        the record's "reported" (the owner's notice); kept only when reports are on, so ones turned on later still tell
        it. One that could not be downloaded or put in place is the pass's own step (STEP_OF: broken, bad-shell)."""
        rec = shellnext.read(self.state_dir)
        sha, at = rec.get("sha256"), rec.get("at")
        if rec.get("status") != "returned" or not isinstance(sha, str) or not isinstance(at, (int, float)) \
                or self.now() - at > self.SHELL_REPORT_AGE or self.reporter.marked("shell") == f"{sha}:returned":
            return
        got = self.reporter.failed("shell", f"runtime {rec.get('runtime') or '?'}", self, "shell-returned",
                                   str(rec.get("why") or "")[:300], extra={"shell": {
                                       k: rec.get(k) for k in ("runtime", "was_runtime", "why", "back", "platform", "then",
                                                               "sha256")}})
        if got != "off":
            self.reporter.mark("shell", f"{sha}:returned")

    def _pending(self):
        """The layer an update left for the restart that has not come yet (not the running one), or None."""
        note = appcode.pending(self.state_dir) or {}
        name = note.get("name")
        return name if isinstance(name, str) and name != appcode.current(self.link(), self.codes()) else None

    def _edits(self, result):
        """No own edits of a clone here: an edited layer is core/appwatch.py's line."""

    def _app_info(self):
        self.info["app"] = self.info["layer"] = True
        self.info.pop("previous", None)
        # with updates off the minimum is not read again: the last one read would hang there for ever
        low = self._below() if (self.cfg.get("update") or {}).get("enabled") else None
        if low:
            self.info["min"] = low
        if self.reporter.configured():   # the page's line "reports of failed updates go to the developers"
            self.info["report"] = {"on": (self.cfg.get("update") or {}).get("report") is not False}
        own = appcode.own_of(self.running()[1])
        if own:   # "About the hub": the release + your edits (N), "Return the released"
            self.info["own"] = {"release": own["release"], "edits": own["edits"]}
        hist = self._history()
        # a switch waits for the restart: the last entry is the running layer itself — no "Return" to it
        if hist and hist[-1].get("version") and not self._pending():
            self.info["previous"] = hist[-1]["version"]

    # --- the minimum version (app spec §7.2)

    def _minimum(self, src):
        """min.json of the distribution server, open (no key: a hub whose key no longer reads must learn it too), into
        layers/min.json; no answer or a busy server — the last one read stays; 404 — there is none."""
        try:
            status, body = self.small(self._url(src, "/min.json"))
        except self.fetch_failed:
            return
        if status == 404:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(self.codes(), MINIMUM))
            return
        if status != 200:
            return
        try:
            doc = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(doc, dict):
            self._layers_write(MINIMUM, doc)

    def _below(self):
        """{version, reason, stops, since?, below: True, ahead?} when the running layer is below the minimum of its
        flavor (ahead: the minimum starts on a later day — a warning before it), else None."""
        name, man = self.running()
        if not man.get("version"):
            return None
        need = minimum_of(self._layers_read(MINIMUM, dict), man.get("flavor") or "team")
        if not need or appcode.version_key(man["version"]) >= appcode.version_key(need["version"]):
            return None
        today = datetime.datetime.fromtimestamp(self.now(), datetime.timezone.utc).date().isoformat()
        return {**need, "below": True, **({"ahead": True} if need.get("since", "") > today else {})}

    # --- the server

    def _auth(self, src):
        return {"Authorization": f"Bearer {src['key']}"} if src["key"] else {}

    def _allow(self, src):
        """The open build's downloads stay on its pages and the hosts of its releases' files, every redirect too: the
        keyword for self.download ({} — the team build's server, as before)."""
        from core import fetch
        return {} if src["key"] else {"allow": lambda url: fetch.asset_ok(url, src["url"])}

    def _url(self, src, path):
        return urllib.parse.urljoin(src["url"] + "/", path.lstrip("/"))

    def _releases(self, src):
        status, body = self.small(self._url(src, "/v1/releases.json"), private=self._auth(src))
        if status >= 500 or status in (408, 429):
            raise Passing(f"releases.json: HTTP {status}")
        if status != 200:
            raise Refused(f"releases.json: HTTP {status}")
        try:
            self.doc = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise Refused("releases.json: not JSON") from None
        return self.doc

    # --- one pass

    def _check(self):
        up = self.cfg.get("update") or {}
        if not up.get("enabled"):
            return "disabled"
        name, man = self.running()
        flavor = man.get("flavor") or (self.app or {}).get("flavor") or "team"   # the shell's: a manifest unread
        src = source(self.state_dir, flavor)
        if src is None:
            return "no-source"   # a shell that carries no address
        if not man.get("version"):
            self.info["error"] = "the running layer has no readable manifest"
            return "error"
        self.prune(name)
        self._shell_written_up()
        self._shell_installed()
        self._minimum(src)
        self.info["update_channel"] = "stable"   # update.channel of hub.json is a hub from git's (core/packaged.apply)
        # a build of the person's edits stands on the release it is over: that one is "current", a newer one installs
        have = (appcode.own_of(man) or {}).get("release") or man["version"]
        self.info["have_version"] = have
        try:
            entry = entry_of(self._releases(src), CHANNEL)
        except self.fetch_failed as e:
            if e.why == "network":   # a state, not a failure: the page says when it last looked (app spec §7.1)
                self.info["error"] = str(e)[:300]
                return "offline"
            raise Refused(str(e)) from None
        if entry is None:
            return "no-release"
        theirs = f"{entry['version']}-{flavor}"
        if not appcode.NAME.match(theirs):   # the folder's name: hub-up would refuse to switch to it
            raise Refused(f"releases.json: the version {entry['version'][:80]!r} is too long for a layer's name")
        self.info.update(version=entry["version"], notes=entry["notes"])
        if appcode.version_key(entry["version"]) <= appcode.version_key(have) or theirs == name:
            return "current"   # never down: from nightly to stable the hub waits for a stable newer than itself
        if self._next() == theirs:
            note = self._layers_read(appcode.NEXT, dict)
            if note.get("ask") is True and self.restart:   # the owner's own install of what waits: his restart now
                self._layers_write(appcode.NEXT, {k: v for k, v in note.items() if k != "ask"})
                self.put("hub.restart_requested", None, {"parts": [], "now": False,
                                                         "why": self._t("update.restart_layer", version=entry["version"])})
            elif note.get("ask") is True:
                self.info["ask_restart"] = True   # installed in the background: the person's restart, not a queued one
            return "restarting"   # installed, the restart waits for busy sessions: nothing to press again
        # did not come up: never offered again, not even by "Check" or `hub update` (app spec §7.1 — the button comes
        # with a newer version); returned or cancelled by the owner: not by the schedule, his own press still installs
        got = self._failed().get(theirs)
        if got is not None and (not self.manual or not isinstance(got, dict) or got.get("why") not in ("owner",)):
            self.info["skipped"] = entry["version"]
            return "current"
        if self._needs_runtime_of(entry["runtime_min"]):
            return self._shell(src, entry, entry["runtime_min"])
        if not self.apply:
            return "available"
        return self._install_layer(src, entry, theirs, name, man)

    def _needs_runtime_of(self, need):
        have = (self.app or {}).get("runtime", 0)
        have = have if isinstance(have, int) and not isinstance(have, bool) else 0
        if have >= need:
            return False
        self.info.update(need_runtime=need, have_runtime=have,
                         **({"download": self.app["download"]} if (self.app or {}).get("download") else {}))
        return True

    def _install_layer(self, src, entry, theirs, before, man):
        """Download, check, unpack; leave it for the restart and ask for it. -> applied | broken | bad-archive |
        needs-runtime | moved."""
        codes = self.codes()
        archives = os.path.join(codes, ARCHIVES)
        os.makedirs(archives, exist_ok=True)
        archive = os.path.join(archives, f"code-{theirs}.tar.gz")
        lay = entry["layer"]
        url = self._url(src, lay["url"])
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(src["url"]).netloc:
            raise Refused("releases.json: the layer is not on the distribution server")
        have = os.path.isfile(archive) and os.path.getsize(archive) == lay["size"] and appcode.sha256(
            archive) == lay["sha256"]
        shown = Progress(self.codes(), entry["version"], lay["size"], fresh(self.state_dir), self.now)
        try:
            return self._take_layer(src, entry, theirs, before, man, archive, url, lay, have, shown)
        finally:
            shown.done()

    def _take_layer(self, src, entry, theirs, before, man, archive, url, lay, have, shown):
        codes = self.codes()
        # unpacked whole by an earlier pass that stopped after it (needs-runtime): neither downloaded nor
        # unpacked again every pass
        whole = self._unpacked(os.path.join(codes, theirs), entry)
        if not have and not whole:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(archive)
            self.stage = "download"
            try:
                self.download(url, archive, lay["sha256"], lay["size"], f"code {entry['version']}", "hub update",
                              private=self._auth(src), progress=shown.download, **self._allow(src))
            except self.fetch_failed as e:
                self.info["error"] = str(e)[:300]
                self.caught = e
                return "bad-archive" if e.why == "checksum" else "broken"
        self.stage = "verify"
        shown.check()
        try:
            got, new = (theirs, whole) if whole else appcode.unpack(archive, codes, expected=theirs)
        except (appcode.Bad, OSError, EOFError, ValueError) as e:   # tarfile's own errors are OSError/EOFError kinds
            with contextlib.suppress(OSError):
                os.unlink(archive)
            self.info["error"] = str(e)[:300]
            self.caught = e
            return "bad-archive"
        finally:
            self.stage = None
        if got != theirs or new.get("version") != entry["version"] or (entry["commit"] and new.get("commit") != entry["commit"]):
            appcode.remove_tree(os.path.join(codes, got))
            self.info["error"] = f"the archive holds {got} ({new.get('commit')}), releases.json names {theirs}"
            return "bad-archive"
        need = new.get("runtime_min", 0) if isinstance(new.get("runtime_min"), int) else 0
        if self._needs_runtime_of(need):
            return self._shell(src, entry, need)
        need = self._min_claude(os.path.join(codes, got))
        if need:   # the release's own Claude Code first (spec d2263524, R3): the layer stays unpacked meanwhile
            ready = self._claude_up(need, hold=True)
            if not ready:
                return "busy" if ready is None else "claude-wait"
        if appcode.current(self.link(), codes) != before:
            return "moved"   # the pointer moved meanwhile (a shell's launcher, a return): the next pass looks again
        return self._stage(entry, got, before, man)

    @staticmethod
    def _unpacked(folder, entry):
        """The release's folder, already whole (its manifest names this version and commit, the files match it): its
        manifest; else None."""
        try:
            man = appcode.load(folder)
        except (appcode.Bad, OSError):
            return None
        if man.get("version") != entry["version"] or (entry["commit"] and man.get("commit") != entry["commit"]):
            return None
        return man if not appcode.diff(folder, man) else None

    def _release(self, name=None, man=None):
        """(name, manifest) of the release the layer name runs — on a build of the person's edits, the release it is
        over, while its layer is there; by default the running layer's."""
        if name is None:
            name, man = self.running()
        own = appcode.own_of(man)
        if own and os.path.isfile(os.path.join(self.codes(), own["layer"], appcode.MANIFEST)):
            return own["layer"], {"version": own["release"], "commit": own["commit"]}
        return name, man

    def _stage(self, entry, name, before, man, own=None):
        """The new layer is whole: the history, the watchdog's trial, next.json — then the restart that moves the pointer.
        own — the person's own build ("own" of its manifest): the way back is the clean release, the restart theirs."""
        from core.restart import FLAG, UP
        self.stage = "switch"
        # the person's edits ran, or a build of them waited for the restart: a release takes them off (read before the
        # switch note below replaces a waiting build's)
        was_own = None if own else appcode.own_of(man) or self._waiting_own()
        # the shell's PACKAGED along (the launcher writes it only into the layer of its own start): the new code knows it
        # is the app — its environment, the download address; not an edit of the layer (appcode.IGNORED)
        mark = os.path.join(self.codes(), before, "PACKAGED")
        if os.path.isfile(mark):
            part = os.path.join(self.codes(), name, "PACKAGED.part")
            shutil.copyfile(mark, part)
            os.replace(part, os.path.join(self.codes(), name, "PACKAGED"))
        was = self._history()
        clean, clean_man = self._release(before, man)   # the history and "Return" name releases, never a build of edits
        hist = [h for h in was if h["name"] not in (before, name, clean)]
        self._layers_write(HISTORY, (hist + [{"name": clean, "version": clean_man.get("version") or ""}])[-base.HISTORY_KEEP:])
        failed = self._failed()
        if name in failed:   # installed on the owner's word (`hub update`): no longer "did not come up"
            failed.pop(name)
            self._layers_write(FAILED, failed)
        shutil.copyfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "trial.py"),
                        os.path.join(self.update_dir(), os.path.basename(base.TRIAL_RUN)))
        up = self._read(UP, dict)
        from core import instance
        back, back_man = (clean, clean_man) if own else (before, man)   # a build that does not come up: the clean release
        trial = {"from": str(back_man.get("commit") or ""), "to": entry["commit"], "version": entry["version"],
                 "previous": back_man.get("version") or "", "started": self.now(), "window": base.TRIAL_WINDOW,
                 "status": "waiting", "port": instance.port("web", self.state_dir),
                 "core_pid": up.get("core") if isinstance(up.get("core"), int) else None, "repo": None,
                 "restart_flag": os.path.join(self.state_dir, FLAG), "up_file": os.path.join(self.state_dir, UP),
                 "layer": {"from": back, "to": name, "dir": self.codes(), "link": self.link()},
                 **self._task()}
        catalog.write_private(os.path.join(self.state_dir, base.TRIAL), trial)
        self._layers_write(appcode.NEXT, {"name": name, "from": before, "at": self.now(), "why": "own" if own else "update",
                                          "history": was, **({} if self.restart and not own else {"ask": True})})
        try:
            self.spawn(os.path.join(self.state_dir, base.TRIAL_RUN), self.state_dir)
        except OSError as e:   # the update stays; the shell's launcher starts the watchdog at the next start
            print(f"update: the watcher did not start: {e!r}", flush=True)
        self.put("update.applied", None, {"from": trial["from"], "to": entry["commit"], "upstream": entry["commit"],
                                          "commits": [], "notes": entry["notes"], "version": entry["version"]})
        # the whole hub, when sessions are free: hub-up moves the pointer between stopping its parts and starting again
        why = self._t("update.restart_layer", version=entry["version"])
        if own:   # the restart is the person's (apply_own): "Restart" under the notice
            line = self._t("update.own_ready", count=own["edits"], version=own["release"])
            self._restart_or_ask(why, line, entry["version"], ask=line)
            return "applied"
        shown = news.bullets(entry["notes"], NOTES_MAX)
        text = self._t("update.applied_version", version=entry["version"], notes=shown)
        ask = self._t("update.ready_restart", version=entry["version"], notes=shown)
        if was_own:   # the clean release takes the edits off: they stay in the working copy
            off = self._t("update.own_dropped", version=entry["version"], count=was_own["edits"])
            text, ask = f"{text}\n\n{off}", f"{ask}\n\n{off}"
        self._restart_or_ask(why, text, entry["version"], shown, ask=ask)
        return "applied"

    # --- the person's own build (`hub app apply`, core/ownsrc.py)

    def _waiting_own(self):
        """"own" of a build left for the restart that has not come (a newer release about to replace it), or None."""
        note = appcode.pending(self.state_dir) or {}
        if note.get("why") != "own" or not isinstance(note.get("name"), str) or not appcode.NAME.match(note["name"]):
            return None
        return appcode.own_of(self._layers_read(os.path.join(note["name"], appcode.MANIFEST), dict))

    def apply_own(self):
        """The build `hub app apply` left (update/own-apply.json): into the layers, then left for the person's restart as
        a release is. -> applied | current | moved (the hub runs another release now) | waits (a release waits for the
        restart) | busy (a release's watchdog counts) | bad; written to update/own-apply.result.json for the command."""
        flag = os.path.join(self.state_dir, ownsrc.FLAG)
        try:   # taken whole first: a request written while this one is read is not deleted unread
            os.replace(flag, flag + ".taken")
        except FileNotFoundError:
            return "none"
        rec = self._read(ownsrc.FLAG + ".taken", dict)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(flag + ".taken")
        stage, name = rec.get("stage"), rec.get("name")
        error = ""
        if not (isinstance(stage, str) and stage.startswith(ownsrc.STAGE) and FILE.match(stage[1:])
                and isinstance(name, str) and appcode.NAME.match(name) and appcode.OWN in name):
            result = "bad"
        else:
            try:
                result = self._apply_own(os.path.join(self.codes(), stage), name)
            except Exception as e:  # noqa: BLE001 — the disk, a record the update cannot read: the command says it
                result, error = "bad", (str(e) if isinstance(e, (OSError, appcode.Bad)) else repr(e))[:300]
        catalog.write_private(os.path.join(self.state_dir, ownsrc.RESULT), {
            "name": name if isinstance(name, str) else "", "result": result, "at": round(self.now()),
            **({"error": error} if error else {})})
        return result

    def _apply_own(self, stage, name):
        self.info, self.manual, self.restart = {}, True, False
        folder = os.path.join(stage, name)
        new = appcode.load(folder)
        own = appcode.own_of(new)
        if not own or appcode.name_of(new) != name or appcode.diff(folder, new, full=True):
            appcode.remove_tree(stage)
            return "bad"
        running, man = self.running()
        if name == running:
            appcode.remove_tree(stage)
            return "current"
        note = appcode.pending(self.state_dir) or {}
        trial = self._read(base.TRIAL, dict)
        if self._release(running, man)[0] != own["layer"]:
            no = "moved"     # the hub runs another release now: the copy merges it first
        elif note.get("why") == "update" and note.get("name") != running:
            no = "waits"     # an installed release waits for the restart: not pushed aside by a build of the older one
        elif trial.get("status") == "waiting" and note.get("why") != "own" and self._watchdog_alive(trial):
            no = "busy"      # a release's watchdog counts: the hub is to come up on it first
        elif self._needs_runtime_of(new.get("runtime_min") if isinstance(new.get("runtime_min"), int) else 0):
            no = "needs-runtime"   # an edit asks for a newer environment than the app's: it would not start
        else:
            no = None
        if no:   # a build is cheap to make again: no full layer left behind for each try
            appcode.remove_tree(stage)
            return no
        need = self._min_claude(folder)
        if need:
            ready = self._claude_up(need, hold=True)
            if not ready:
                appcode.remove_tree(stage)   # a build is cheap to make again
                return "busy" if ready is None else "claude-wait"
        target = os.path.join(self.codes(), name)
        if os.path.lexists(target):
            appcode.remove_tree(target)
        os.replace(folder, target)
        archive = os.path.join(stage, f"code-{name}.tar.gz")
        if os.path.isfile(archive):   # the launcher's copy to put an edited layer back from
            os.makedirs(os.path.join(self.codes(), ARCHIVES), exist_ok=True)
            os.replace(archive, os.path.join(self.codes(), ARCHIVES, f"code-{name}.tar.gz"))
        appcode.remove_tree(stage)
        line = self._t("update.own_ready", count=own["edits"], version=own["release"])
        got = self._stage({"version": new["version"], "commit": new.get("commit") or "", "notes": [line]}, name, running,
                          man, own=own)
        self.info["version"] = new["version"]
        self._app_info()   # "About the hub": "Restart", no "Return" to a release while the build waits
        self.put("update.checked", None, {"result": got, **self.info})
        return got

    def _task(self):
        """{"task": <the hub's scheduler task>} on Windows (the watchdog runs it when no hub-up is left after a rollback,
        trial.start_hub), else {}."""
        if os.name != "nt":
            return {}
        try:
            unit = platforms.current.service_unit(self.state_dir)
        except Exception:  # noqa: BLE001 — the scheduler not answering: the watchdog just has no task to run
            return {}
        return {"task": unit["id"]} if isinstance(unit, dict) and isinstance(unit.get("id"), str) else {}

    READY_ID = "update-ready"   # the notice of an installed release waiting for the restart: a newer one edits it

    def _restart_or_ask(self, why, text, version, notes="", ask=None):
        """An installed release (a layer, an environment) waits for the full restart. The owner's install and the first
        start restart the hub when sessions are free, as before; a background install asks the person (the owner's
        decision of 2026-11-09, m1917410): one notice "updated to X — restart the hub" (`ready`: the page and Telegram
        put a "Restart" button on it), the same id, so a newer release waiting edits it instead of a second one."""
        if self.restart:
            self.put("hub.restart_requested", None, {"parts": [], "now": False, "why": why})
            self.put("owner.notify", None, {"title": self._t("core.update.title"), "text": text})
            return
        self.info["ask_restart"] = True   # the page: "Restart" (the owner's own install has its restart queued already)
        self.put("owner.notify", None, {"title": self._t("core.update.title"), "id": self.READY_ID,
                                        "ready": {"version": version},
                                        "text": ask or self._t("update.ready_restart", version=version, notes=notes)})

    # --- the new app (a release that needs a newer environment, app spec §4.4)

    def _platform(self):
        return str((self.app or {}).get("platform") or "")

    def _folder(self):
        """The one-line install's environment folder this hub runs on, or None (an app, a package)."""
        return shellnext.folder_env(self.state_dir)

    def _shell_offer(self, need):
        """The new app of this platform in releases.json (runtime.apps; the one-line install — the environment archive
        of runtime.shells), checked: {url, sha256, size, runtime, name}; None — there is none with this environment, or
        it did not come up here before."""
        key = "shells" if self._folder() else "apps"
        apps = ((self.doc or {}).get("runtime") or {}).get(key) if isinstance((self.doc or {}).get("runtime"), dict) else None
        got = apps.get(self._platform()) if isinstance(apps, dict) and self._platform() else None
        if not (isinstance(got, dict) and isinstance(got.get("url"), str) and isinstance(got.get("sha256"), str)
                and SHA.match(got["sha256"]) and type(got.get("size")) is int and 0 < got["size"] <= 2 * 1024 ** 3
                and type(got.get("runtime")) is int and got["runtime"] >= need):
            return None
        name = os.path.basename(urllib.parse.urlsplit(got["url"]).path)
        if not FILE.match(name) or f"shell:{got['sha256']}" in self._failed():
            return None
        return {**got, "name": name}

    def _swapper(self):
        """(the shell's Python, its shellswap.py) — None for a shell built before it (the page keeps the link); an
        environment folder — one whose watchdog moves a pointer."""
        shell = self._layers_read("shell.json", dict)
        script, python = shell.get("launch"), shell.get("python")
        if not (isinstance(script, str) and isinstance(python, str)):
            return None
        swap = os.path.join(os.path.dirname(script), "shellswap.py")
        if not os.path.isfile(swap):
            return None
        if self._folder():   # its watchdog moves a pointer (the factory says so in its PACKAGED since slice O4)
            got = shellnext._json(os.path.join(os.path.dirname(script), "PACKAGED")).get("swaps")
            if not isinstance(got, list) or "folder" not in got:
                return None
        return python, swap

    def _shell(self, src, entry, need):
        """needs-runtime with the new app on the server: offered with "Update"; pressed — downloaded and laid out."""
        offer = self._shell_offer(need)
        if offer is None:
            return "needs-runtime"   # the link of the shell's PACKAGED, as before
        rec = shellnext.read(self.state_dir)
        linux = shellnext.kind(self._platform()) == "linux" and not self._folder()
        # auto: "Update" does it (Mac, Windows: puts it in place; Linux: downloads the .deb — deb — for apt)
        self.info["shell"] = {"runtime": offer["runtime"], "size": offer["size"],
                              "auto": linux or self._swapper() is not None, **({"deb": True} if linux else {})}
        if rec.get("sha256") == offer["sha256"] and rec.get("status") in ("ready", "swapping", "swapped"):
            if rec.get("ask") is True:
                self.info["ask_restart"] = True   # laid out in the background: the person's restart
            return "shell-ready"   # pressed already: the restart takes it
        if linux and rec.get("sha256") == offer["sha256"] and rec.get("status") == "downloaded" and os.path.isfile(
                str(rec.get("file"))):
            self.info["shell"]["file"] = rec["file"]
            return "shell-downloaded"
        if not self.apply or not self.info["shell"]["auto"]:   # a layer's pass installs in the background (update.plan)
            return "needs-runtime"
        return self._install_shell(src, offer, entry, need, linux)

    def _install_shell(self, src, offer, entry, need, linux):
        url = self._url(src, offer["url"])
        if src["key"] is None:   # the open build: on its pages or an asset of its GitHub releases
            from core import fetch
            if not fetch.asset_ok(url, src["url"]):
                raise Refused("releases.json: the app is neither on the pages nor among the releases' files")
        elif urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(src["url"]).netloc:
            raise Refused("releases.json: the app is not on the distribution server")
        if self._folder():
            return self._install_env(url, src, offer, entry)
        shells = os.path.join(self.codes(), SHELLS)
        os.makedirs(shells, exist_ok=True)
        dest = os.path.join(shells, offer["name"])
        if not (os.path.isfile(dest) and os.path.getsize(dest) == offer["size"] and appcode.sha256(dest) == offer["sha256"]):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(dest)
            try:
                self.download(url, dest, offer["sha256"], offer["size"], f"the app {offer['name']}", "hub update",
                              private=self._auth(src), **self._allow(src))
            except self.fetch_failed as e:
                self.info["error"] = str(e)[:300]
                return "bad-archive" if e.why == "checksum" else "broken"
        base_rec = {"platform": self._platform(), "runtime": offer["runtime"], "was_runtime": (self.app or {}).get("runtime", 0),
                    "sha256": offer["sha256"], "file": dest, "then": entry["version"], "at": self.now()}
        if linux:   # root installs a .deb: the hub names the command, and takes the new app at its next restart
            shellnext.write(self.state_dir, {**base_rec, "status": "downloaded"})
            self.info["shell"]["file"] = dest
            self.put("owner.notify", None, {"title": self._t("core.update.title"), "text": self._t(
                "core.update.shell_downloaded", runtime=offer["runtime"], file=dest)})
            return "shell-downloaded"
        python, swap = self._swapper()
        shellnext.write(self.state_dir, {**base_rec, "status": "preparing"})
        try:
            r = self.run([python, "-I", "-B", swap, "prepare", self.state_dir, dest, str(offer["runtime"])],
                         capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=1200)
            code, said = r.returncode, (r.stderr or r.stdout or "").strip()
        except (OSError, subprocess.SubprocessError) as e:
            code, said = 1, str(e)
        rec = shellnext.read(self.state_dir)
        if code or rec.get("status") != "ready":
            shellnext.write(self.state_dir, {**base_rec, "status": "failed", "why": said[-300:], "reported": True})
            with contextlib.suppress(OSError):
                os.unlink(dest)
            self.info["error"] = said[-300:] or f"exit code {code}"
            return "bad-shell"
        shellnext.write(self.state_dir, {**rec, **{k: base_rec[k] for k in ("sha256", "then", "was_runtime")},
                                         **({} if self.restart else {"ask": True})})
        line = self._t("core.update.shell_ready", runtime=offer["runtime"], version=entry["version"])
        self._restart_or_ask(self._t("update.restart_shell", runtime=offer["runtime"]), line, entry["version"],
                             ask=line + "\n" + self._t("update.ready_restart_shell"))
        return "shell-ready"

    def _env_target(self, shells, offer, python, unpack):
        """(the folder the new environment goes to, True when it is there already and whole): the one the install line
        left for the update (core/envs.py keep_next) or <runtime>-<flavor> when whole and of this runtime; else that
        name, or a new one next to it when that one is there and broken (a process may hold its files)."""
        from core import envs
        flavor = str((self.app or {}).get("flavor") or "team")
        name = f"{offer['runtime']}-{flavor}"
        path = os.path.join(shells, name)
        left = shellnext._json(os.path.join(self.codes(), envs.NEXT)).get("file")
        for cand in ([left] if isinstance(left, str) else []) + [path]:
            if not (os.path.isdir(cand) and os.path.normcase(os.path.dirname(os.path.realpath(cand))) ==
                    os.path.normcase(os.path.realpath(shells))):
                continue
            mark = shellnext._json(os.path.join(cand, "PACKAGED"))
            if mark.get("runtime") != offer["runtime"] or mark.get("flavor", flavor) != flavor:
                continue
            if self._call([python, "-I", "-B", unpack, "check", cand], 900)[0] == 0 and \
                    os.path.isfile(os.path.join(cand, ".agents-hub-sealed")):
                return cand, True
        if not os.path.lexists(path):
            return path, False
        n = int(self.now())
        while os.path.lexists(os.path.join(shells, f"{name}-{n}")) or os.path.lexists(os.path.join(shells, f"{name}-{n}.part")):
            n += 1
        return os.path.join(shells, f"{name}-{n}"), False

    def _call(self, cmd, timeout, env=None):
        try:
            r = self.run(cmd, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                         **({"env": env} if env else {}))
            return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
        except (OSError, subprocess.SubprocessError) as e:
            return 1, str(e)

    def _install_env(self, url, src, offer, entry):
        """The new environment of the one-line install: the archive into <data>/downloads, unpacked and checked next to
        the current environment by the scripts' own program, its installer's own steps (install.py prepare), then laid
        out for the full restart by the current environment's watchdog (shellswap.py prepare)."""
        env = self._folder()
        shells = os.path.dirname(env)
        python, swap = self._swapper()
        unpack = os.path.join(self.repo, "deploy", "oneline", "unpack.py")
        base_rec = {"platform": self._platform(), "runtime": offer["runtime"],
                    "was_runtime": (self.app or {}).get("runtime", 0), "sha256": offer["sha256"],
                    "then": entry["version"], "at": self.now()}
        target, whole = self._env_target(shells, offer, python, unpack)
        shellnext.write(self.state_dir, {**base_rec, "status": "preparing", "file": target})
        from core import envs
        left = shellnext._json(os.path.join(self.codes(), envs.NEXT)).get("file")
        if isinstance(left, str) and os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(target)):
            with contextlib.suppress(FileNotFoundError):   # the record names it from now on
                os.unlink(os.path.join(self.codes(), envs.NEXT))
        if not whole:
            downloads = os.path.join(os.path.dirname(shells), "downloads")
            os.makedirs(downloads, exist_ok=True)
            archive = os.path.join(downloads, offer["name"])
            if not (os.path.isfile(archive) and os.path.getsize(archive) == offer["size"]
                    and appcode.sha256(archive) == offer["sha256"]):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(archive)
                try:
                    self.download(url, archive, offer["sha256"], offer["size"], f"the environment {offer['name']}",
                                  "hub update", private=self._auth(src), **self._allow(src))
                except self.fetch_failed as e:
                    shellnext.write(self.state_dir, {**base_rec, "status": "failed", "why": str(e)[:300], "reported": True})
                    self.info["error"] = str(e)[:300]
                    return "bad-archive" if e.why == "checksum" else "broken"
            mark = {"platform": self._platform(), "flavor": (self.app or {}).get("flavor") or "team",
                    "runtime": offer["runtime"], "archive": offer["name"], "sha256": offer["sha256"],
                    "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            part = target + ".part"
            with contextlib.suppress(OSError):
                if os.path.isdir(part):
                    from core import envs
                    envs.remove(part)
            code, said = self._call([python, "-I", "-B", unpack, "unpack", archive, part, target], 1800,
                                    env={**os.environ, "HUB_SHELL_MARK": json.dumps(mark)})
            if code == 0:
                code, said = self._call([shellnext.python_of(target), "-I", "-B", os.path.join(target, "install.py"),
                                         "prepare", "--env", target], 1800)
            if code:
                with contextlib.suppress(OSError):
                    from core import envs
                    for p in (part, target):
                        if os.path.isdir(p):
                            envs.remove(p)
                return self._shell_refused(base_rec, said, code)
            with contextlib.suppress(OSError):
                os.unlink(archive)
        code, said = self._call([python, "-I", "-B", swap, "prepare", self.state_dir, target, str(offer["runtime"])], 1200)
        rec = shellnext.read(self.state_dir)
        if code or rec.get("status") != "ready":
            return self._shell_refused(base_rec, said, code)
        shellnext.write(self.state_dir, {**rec, **{k: base_rec[k] for k in ("sha256", "then", "was_runtime")},
                                         **({} if self.restart else {"ask": True})})
        line = self._t("core.update.shell_ready", runtime=offer["runtime"], version=entry["version"])
        self._restart_or_ask(self._t("update.restart_shell", runtime=offer["runtime"]), line, entry["version"],
                             ask=line + "\n" + self._t("update.ready_restart_shell"))
        return "shell-ready"

    def _shell_refused(self, base_rec, said, code):
        shellnext.write(self.state_dir, {**base_rec, "status": "failed", "why": said[-300:], "reported": True})
        self.info["error"] = said[-300:] or f"exit code {code}"
        return "bad-shell"

    def _shell_installed(self):
        """A newer app on disk than the one this hub came up from (a .deb installed by the person, an app dragged in):
        the restart that takes it, asked once for that app (Windows: its Setup.exe restarts the hub itself)."""
        if not platforms.current.APP_SWAP_ON_CHANGE or not shellnext.changed(self.state_dir, self.repo):
            return
        disk = shellnext._json(os.path.join(shellnext.shell_dir(self.state_dir), "PACKAGED"))
        key = f"{disk.get('version')}:{disk.get('runtime')}"
        if self._layers_read(SHELL_ASKED, dict).get("key") == key:
            return
        self._layers_write(SHELL_ASKED, {"key": key, "at": self.now()})
        self.put("hub.restart_requested", None, {"parts": [], "now": False, "why": self._t(
            "update.restart_shell", runtime=disk.get("runtime") or "?")})

    def _shell_written_up(self):
        """A replacement of the app that ended (shellnext.finished; Linux: the downloaded .deb is the running app now)
        is told to the owner once; done with a release waiting for it — that release is installed next."""
        rec = shellnext.read(self.state_dir)
        have = (self.app or {}).get("runtime", 0)
        if rec.get("status") == "downloaded" and isinstance(rec.get("runtime"), int) and isinstance(have, int) \
                and have >= rec["runtime"]:
            rec["status"] = "done"
        if rec.get("status") not in ("done", "returned", "failed") or rec.get("reported"):
            return None
        runtime = rec.get("runtime") or "?"
        if rec["status"] == "done":
            line = self._t("core.update.shell_done", runtime=runtime)
            self.put("update.checked", None, {"result": "current", "app": True, "layer": True})
        elif rec["status"] == "returned":
            failed = self._failed()
            if isinstance(rec.get("sha256"), str):
                failed[f"shell:{rec['sha256']}"] = {"runtime": runtime, "at": round(self.now()), "why": rec.get("why")}
                self._layers_write(FAILED, failed)
            line = self._t("core.update.shell_returned" if rec.get("back", True) else "core.update.shell_not_back",
                           runtime=runtime, was=rec.get("was_runtime") or "?")
            self.put("update.failed", None, {"why": "shell-returned", "by": "timeout", "notes": [line]})
        else:
            line = self._t("core.update.shell_failed", runtime=runtime, why=str(rec.get("why") or "?")[:300])
            self.put("update.failed", None, {"why": "shell-failed", "notes": [line]})
        shellnext.write(self.state_dir, {**rec, "reported": True})
        self.put("owner.notify", None, {"title": self._t("core.update.title"), "text": line})
        if rec["status"] == "done" and rec.get("then"):   # the release that waited for this app: installed next pass
            # the swap the owner asked for installs its release as his (restarting); a background one asks again
            flag = base.NOW_FLAG if rec.get("ask") is True else base.INSTALL_FLAG
            with open(os.path.join(self.state_dir, flag), "w"):
                pass
        return rec["status"]

    def _shell_prune(self):
        """The downloaded apps and the old app's copy once a replacement is written up and its watchdog has left."""
        rec = shellnext.read(self.state_dir)
        if rec and not rec.get("reported") and rec.get("status") != "downloaded":
            return
        if rec and isinstance(rec.get("pid"), int) and platforms.current.alive(rec["pid"]):
            return   # its watchdog still runs from the old app's copy
        if rec.get("status") == "downloaded":
            return   # the .deb waits for the person
        shells = os.path.join(self.codes(), SHELLS)
        for n in os.listdir(shells) if os.path.isdir(shells) else []:
            path = os.path.join(shells, n)
            with contextlib.suppress(OSError):
                if n.endswith(".part") and self.now() - os.path.getmtime(path) < 24 * 3600:
                    continue   # a download that broke off continues at the next press
                os.unlink(path)
        if self._folder():   # the environments no hub of the machine needs any more
            from core import envs
            with contextlib.suppress(OSError):
                envs.prune(self.state_dir, now=self.now, say=lambda t: print(f"update: {t}", flush=True))
        if rec.get("status") == "returned" and rec.get("back") is False:
            return   # the old app's copy may be the only working one left: the owner's (logs/shell-update.log)
        for key in ("old", "new"):
            path = rec.get(key) if rec else None
            if isinstance(path, str) and path.endswith((".old", ".new")) and os.path.isdir(path):
                with contextlib.suppress(OSError):
                    appcode.remove_tree(path)
        app = rec.get("app") if rec else None
        if isinstance(app, str) and os.path.isdir(app + ".bad"):
            with contextlib.suppress(OSError):
                appcode.remove_tree(app + ".bad")

    # --- after a restart, a return, the disk

    def recover(self):
        """At the process start: a rollback by the watchdog is written up; a trial whose watchdog is gone (a reboot)
        gets it again; a switch note whose layer is gone is dropped."""
        nxt = self._next()
        if nxt and not os.path.isfile(os.path.join(self.codes(), nxt, appcode.MANIFEST)):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(self.codes(), appcode.NEXT))
        note = os.path.join(self.state_dir, base.ROLLED)
        trial = self._read(base.TRIAL, dict)
        done = self._read(base.ROLLED, dict)
        layer = trial.get("layer") if isinstance(trial.get("layer"), dict) else {}
        if done.get("layer_to"):
            midway = trial.get("status") == "waiting" and layer.get("to") == done["layer_to"] and self._watchdog_alive(trial)
            if not midway:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(note)
                if appcode.current(self.link(), self.codes()) == done.get("layer_from"):
                    if trial.get("status") == "waiting" and layer.get("to") == done["layer_to"]:
                        catalog.write_private(os.path.join(self.state_dir, base.TRIAL), {**trial, "status": "rolled-back"})
                    self.info, self.manual = {}, False
                    self._rolled_back_layer(done["layer_to"], str(done.get("version") or ""), done["layer_from"],
                                            str(done.get("previous") or ""), "timeout")
        if trial.get("status") == "waiting" and not self._watchdog_alive(trial) and os.path.exists(
                os.path.join(self.state_dir, base.TRIAL_RUN)):
            try:
                self.spawn(os.path.join(self.state_dir, base.TRIAL_RUN), self.state_dir, "--resume")
            except OSError as e:
                print(f"update: the watcher did not start: {e!r}", flush=True)
        try:   # the app's replacement that ended while no hub ran: the owner hears of it now
            self.info, self.manual = {}, False
            self._shell_written_up()
        except OSError as e:
            print(f"update: the app's replacement is not written up: {e!r}", flush=True)

    def _rolled_back_layer(self, bad, version, prev, previous, by):
        failed = self._failed()
        failed[bad] = {"version": version, "at": round(self.now()), "why": by}
        self._layers_write(FAILED, failed)   # first: a write-up cut off later must not offer it again
        self._layers_write(HISTORY, [h for h in self._history() if h["name"] not in (prev, bad)])
        names = {"version": version or bad, "previous": previous or prev}
        own = appcode.OWN in bad   # the person's own build: their edits, not a release of the developers'
        back = appcode.own_of(self._layers_read(os.path.join(prev, appcode.MANIFEST), dict))
        if not own and back and by == "timeout":   # back on what ran: the build of the edits, not "the edits are off"
            line = self._t("core.update.layer_not_up_own", version=version or bad, release=back["release"],
                           count=back["edits"])
        else:
            line = self._t(("core.update.own_not_up" if by == "timeout" else "core.update.own_off") if own else
                           "core.update.layer_not_up" if by == "timeout" else "core.update.rolled_back_owner", **names)
        self.put("update.failed", None, {"why": "rolled-back", "by": by, "from": bad, "to": prev, "notes": [line],
                                         **({"version": version} if version else {})})
        self.put("owner.notify", None, {"title": self._t("core.update.title"), "text": line})
        if own and by == "timeout":   # the Hub Developer's to fix: what did not come up and where the logs are
            self._send(OWN_NOT_UP_DEV.format(name=bad, previous=previous or prev, logs=os.path.join(self.state_dir, "logs")))
        elif by == "timeout":   # the hub did not come up on it: the previous code writes it up to the developers too
            try:
                self.reporter.failed("trial", version or bad, self, "rolled-back",
                                     f"the hub did not come up on {bad} in time; back on {prev}")
            except Exception as e:  # noqa: BLE001 — the write-up is done; the report is a side job
                print(f"update: the report failed: {e!r}", flush=True)

    def roll_back(self, expect=""):
        """"Return <previous>" (`hub update rollback`): the previous layer at the next restart, the returned one is not
        offered again. 'rolled-back' | 'no-previous' | 'moved' (not the one expected)."""
        self.info, self.manual = {}, True
        staged = self._pending()
        running = self.running()[1].get("version")
        # an installed version waits for the restart: "Return" is to the running one — the switch is taken back
        if staged and (appcode.pending(self.state_dir) or {}).get("why") in ("update", "own") and (
                not expect or expect.removeprefix("v") == running):
            return self._cancel(staged)
        hist = [h for h in self._history() if os.path.isfile(os.path.join(self.codes(), h["name"], appcode.MANIFEST))]
        if expect and hist and (staged or hist[-1].get("version") != expect.removeprefix("v")):
            self.put("owner.notify", None, {"title": self._t("core.update.title"), "text": self._t(
                "core.update.rollback_moved", version=expect, previous=hist[-1].get("version") or hist[-1]["name"])})
            self._app_info()
            self.put("update.checked", None, {"result": "current", **self.info})
            return "moved"
        if not hist:
            self.put("owner.notify", None, {"title": self._t("core.update.title"),
                                            "text": self._t("core.update.no_previous")})
            return "no-previous"
        prev = hist[-1]
        bad, man = self.running()
        # the switch back first, the watchdog let go after it: a write that fails leaves the watchdog on guard (review
        # 02.11: marked first, a failed write left the hub on the release it was leaving with nobody watching)
        self._layers_write(appcode.NEXT, {"name": prev["name"], "from": bad, "at": self.now(), "why": "return"})
        trial = self._read(base.TRIAL, dict)
        if trial.get("status") == "waiting":   # its watchdog leaves (decide: done)
            catalog.write_private(os.path.join(self.state_dir, base.TRIAL), {**trial, "status": "rolled-back"})
            from modules.update import trial as watch
            watch.remove(os.path.join(self.state_dir, base.ROLLED))
        self._rolled_back_layer(bad, man.get("version") or "", prev["name"], prev.get("version") or "", "owner")
        self.info = {"version": prev.get("version") or prev["name"]}
        self._app_info()
        self.put("update.checked", None, {"result": "rolled-back", **self.info})
        self.put("hub.restart_requested", None, {"parts": [], "now": False, "why": self._t(
            "update.restart_rollback", bad=man.get("version") or bad, previous=prev.get("version") or prev["name"])})
        return "rolled-back"

    def _cancel(self, staged):
        """"Return" while an installed version waits for the restart: the switch is taken back, the hub stays where it
        is; the cancelled version is not offered again by the schedule (as a returned one)."""
        name, man = self.running()
        trial = self._read(base.TRIAL, dict)
        if trial.get("status") == "waiting":
            catalog.write_private(os.path.join(self.state_dir, base.TRIAL), {**trial, "status": "rolled-back"})
        note = appcode.pending(self.state_dir) or {}
        was = note.get("history")   # the history before the install (it may have dropped an older entry)
        self._layers_write(HISTORY, [h for h in was if isinstance(h, dict) and isinstance(h.get("name"), str)]
                           if isinstance(was, list) else [h for h in self._history() if h["name"] != name])
        version = self._layers_read(os.path.join(staged, appcode.MANIFEST), dict).get("version") or staged
        failed = self._failed()
        failed[staged] = {"version": version, "at": round(self.now()), "why": "owner"}
        self._layers_write(FAILED, failed)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(self.codes(), appcode.NEXT))
        line = self._t("core.update.cancelled", version=version, current=man.get("version") or name)
        self.put("update.failed", None, {"why": "cancelled", "by": "owner", "from": staged, "to": name, "notes": [line],
                                         "version": version})
        self.put("owner.notify", None, {"title": self._t("core.update.title"), "text": line})
        self.info = {"version": man.get("version") or name, "have_version": man.get("version") or name,
                     "update_channel": "stable"}
        self._app_info()
        self.put("update.checked", None, {"result": "current", **self.info})
        return "cancelled"

    def prune(self, name):
        """Old layers and their archives off the disk: kept are the current, the one left for the restart, the history,
        the last copy of an edit. A layer a process still holds (Windows) stays for the next pass."""
        codes = self.codes()
        keep = {name, self._next(), *(h["name"] for h in self._history())}
        trial = self._read(base.TRIAL, dict)
        if trial.get("status") == "waiting" and isinstance(trial.get("layer"), dict):   # the watchdog's way back (review)
            keep.update(v for v in (trial["layer"].get("from"), trial["layer"].get("to")) if isinstance(v, str))
        keep.discard(None)
        for n in [n for n in keep if appcode.OWN in n]:   # a build of the person's edits: its release is its way back
            keep.add(self._release(n, self._layers_read(os.path.join(n, appcode.MANIFEST), dict))[0])
        with contextlib.suppress(OSError):   # the hub's random id of "where it stands" (A3n, gone 09.11): not kept
            os.unlink(os.path.join(codes, "stand.json"))
        try:
            names = os.listdir(codes)
        except OSError:
            return
        # the newest copy of an edit by its stamp (<layer>-modified-<YYYYmmdd-HHMMSS>), not by the name: "1.10.0-…" sorts
        # before "1.9.3-…" (review)
        copies = sorted((n for n in names if appcode.MODIFIED in n), key=lambda n: n.rsplit(appcode.MODIFIED, 1)[1])
        running = self._layers_read(os.path.join(name, appcode.MANIFEST), dict).get("version") or ""
        failed = self._failed()
        newer = [(appcode.version_key(v), n) for n in names if n not in failed and appcode.OWN not in n   # no release
                 and isinstance(v := self._layers_read(os.path.join(n, appcode.MANIFEST), dict).get("version"), str)
                 and appcode.version_key(v) > appcode.version_key(running)]
        waiting = max(newer)[1] if newer else None   # only the newest one waits: a withdrawn or passed-over one goes
        for n in names:
            path = os.path.join(codes, n)
            if n.startswith(ownsrc.STAGE) and os.path.isdir(path):   # a build `hub app apply` left and nobody took
                with contextlib.suppress(OSError):
                    if self.now() - os.stat(path).st_mtime > STAGE_KEEP:
                        appcode.remove_tree(path)
                continue
            if n in keep or n in copies[-1:] or not os.path.isdir(path) or os.path.islink(path):
                continue
            # a newer release unpacked by a pass that stopped after it (needs-runtime): the next pass
            # takes it from here instead of downloading and unpacking it again
            if n == waiting:
                continue
            if not (appcode.NAME.match(n) and (os.path.isfile(os.path.join(path, appcode.MANIFEST)) or appcode.MODIFIED in n)):
                continue
            try:
                appcode.remove_tree(path)
            except OSError as e:
                print(f"update: an old layer stays for now ({n}): {e!r}", flush=True)
        self._shell_prune()
        archives = os.path.join(codes, ARCHIVES)
        for n in os.listdir(archives) if os.path.isdir(archives) else []:
            m = re.fullmatch(r"code-(.+)\.tar\.gz(?:\.part)?", n)
            if m and m.group(1) not in keep and not n.endswith(".part"):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(archives, n))


PROGRESS = "progress.json"
PROGRESS_EVERY = 0.5   # s: the band asks every 2 s; a file per chunk would be thousands of writes


class Progress:
    """layers/progress.json for the page's band (app spec §4a.2: "Updating to X: downloading 6 of 11 MB" → "checking"):
    {version, phase download|check, done, total, first, ts}; first — the first start's install (§4a.1 step 2), not an
    update. The file goes when the install ends, whatever the end (modules/setup/setup.layer_progress reads it)."""

    def __init__(self, codes, version, total, first, now=time.time):
        self.path, self.now, self.at = os.path.join(codes, PROGRESS), now, 0.0
        self.base = {"version": version, "total": total, "first": bool(first)}

    def write(self, **kw):
        with contextlib.suppress(OSError):   # what the band shows: never a reason for an update to fail
            catalog.write_private(self.path, {**self.base, **kw, "ts": round(self.now(), 1)})

    def download(self, done, total):
        if done < total and self.now() - self.at < PROGRESS_EVERY:
            return
        self.at = self.now()
        self.write(phase="download", done=done, total=total)

    def check(self):
        self.write(phase="check", done=self.base["total"])

    def done(self):
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(self.path)


def first_wait(failures):
    """Seconds before the next try of the first start's install after this many failed ones in a row: none before the
    first try, then a minute, doubling, at most the update's own 6 h (a machine offline, a revoked key: not a fact
    every minute for ever)."""
    return 0 if failures <= 0 else min(60 * 2 ** min(failures - 1, 20), 6 * 3600)


def fresh(state_dir):
    """The shell's launcher installed the first layer of this hub (layers/fresh.json): the update installs the
    channel's latest without a button."""
    return os.path.isfile(os.path.join(layers_dir(state_dir), FRESH))


def first_start(updater):
    """The first start's install (app spec §4.3, §4a.1 step 2). -> the result; the record goes unless the server could
    not be read (the next pass tries again)."""
    result = updater.check(apply=True, manual=True)
    if result not in ("offline", "broken", "error", "claude-wait", "busy"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(layers_dir(updater.state_dir), FRESH))
    return result


def on_layer(repo, state_dir):
    """The code runs from a code layer of this state folder (the app spec's layout), not from git."""
    return appcode.on_layer(state_dir, repo)
