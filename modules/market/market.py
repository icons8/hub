"""Hub plugins by the old path: a code check by link, and plugins installed outside the catalogs. Catalogs with
pinned commits are `hub marketplace` (core/marketplaces.py, core/mpinstall.py).

A plugin is a GitHub repo with a `hub-plugin.json` file in the root:

  {"name": "mascot", "description": "what it does", "web": "main.js", "run": ["python3", "main.py"], "skills": true}

`web` is a page extension, `run` is a process next to the core (EXTENDING.md), `skills` means the repo is also a Claude
Code skill set (`.claude-plugin/marketplace.json`). Outside a catalog a plugin is installed only through a code check:
a clone at a commit, a Hub Developer verdict, installing exactly that commit into `<state>/market/<account>/<repo>` and
a line in `hub.json` (`extensions` and/or `sets`) with the source.

  hub market review <GitHub link> — the repo goes to Hub Developer for a check; install it with install <repo> --reviewed
  hub market update <name> [--reviewed] | remove <name> — what is installed; from a catalog — through the catalog
  hub market search|list|install … — the same as hub marketplace (the hint prints the new command)
  install|update … --accept — the update asks for more rights than you allowed (a process or another command of it,
  page code, an executable of a skill set): without --accept it is not installed, the previous version keeps working
  (core/plugrights.py)

An installed plugin is called with the author's namespace — `<account>:<name>`: equal names of different authors do not
clash and do not touch personal extensions (theirs have no colon). update/remove also take the short form — `mascot`.

Plugin code runs on the machine with your access — the hub installs it only on the owner's word (the hub skill).
"""
import functools
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request

import platforms
from core import catalog, i18n, net, plugrights, version  # noqa: F401 — net: go to the host directly, bypassing the machine proxy
try:
    from core import pluginstyle
except ImportError:   # the previous release's page process imports this file from the new tree while its own core has no such file: no css field
    pluginstyle = None

STORE = "market"   # <state>/market: the former plugin storage and review clones
MANIFEST = "hub-plugin.json"
# keys of a plugin record in hub.json written by the market (from the manifest and the clone); the rest are the owner's settings
FROM_MANIFEST = ("web", "run", "themes", "css", "hub", "version")
MANAGED = {"path", "source", "commit", "disabled", "approved", "market", "entry", "subdir", *FROM_MANIFEST}
ACCOUNT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
KEY = re.compile(r"[a-z0-9][a-z0-9-]*(?::[a-z0-9][a-z0-9-]*)?")  # extension name: its own or <account>:<name>
FILE = re.compile(r"[A-Za-z0-9_./-]+")

ICON_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp"}
ICON_MAX = 256 * 1024


class Error(Exception):
    pass


class NeedsConsent(Error):
    """The new version asks for more than the owner allowed: `wider` is the new rights as pairs (core/plugrights.py), `shown` is
    the same in words. Nothing was installed, the previous version keeps running."""

    def __init__(self, text, wider):
        super().__init__(text)
        self.wider, self.shown = wider, [plugrights.shown(w) for w in wider]


def consent_text(key, commit, wider, again):
    """Refusal without consent: what the new version asks for and how to allow it (again is the whole command)."""
    return i18n.t("cli.market.wider_consent", name=key, commit=commit[:7],
                  rights="; ".join(plugrights.shown(w) for w in wider), again=again)


@functools.lru_cache(maxsize=1)
def _token():
    """The owner's GitHub key if there is one: GITHUB_TOKEN/GH_TOKEN or the `gh` login. With it GitHub gives 5000
    requests an hour instead of 60, so installs stop hitting "403 rate limit". None: requests go without a key."""
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k, "").strip():
            return os.environ[k].strip()
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def github(url):
    """GET to the GitHub API, JSON; a file goes through contents (raw). The owner's key (_token) goes only to
    api.github.com; without one, public repos only and 60 requests an hour."""
    headers = {"User-Agent": "agents-hub-core", "Accept": (
        "application/vnd.github.raw+json" if "/contents/" in url else "application/vnd.github+json")}
    req = urllib.request.Request(url, headers=headers)
    if url.startswith("https://api.github.com/") and (token := _token()):
        # unredirected: urllib carries ordinary headers to wherever a redirect points (codeload, another host)
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _file(x):
    return isinstance(x, str) and FILE.fullmatch(x) and ".." not in x.split("/")


def valid(man):
    """The plugin manifest or None: the name is Latin, paths stay inside the repo, the command is a list of strings."""
    if not isinstance(man, dict) or not NAME.fullmatch(str(man.get("name") or "")):
        return None
    web, run, themes = man.get("web"), man.get("run"), man.get("themes")
    if web is not None and not _file(web):
        return None
    if run is not None and (not isinstance(run, list) or not run or not all(isinstance(x, str) for x in run)):
        return None
    if themes is not None and (not isinstance(themes, list) or not all(_file(t) for t in themes)):
        return None
    # css (page API 1.37): stylesheets of the page plugin that the page puts into its <head>; a bad entry is dropped there
    # (core/pluginstyle.py), not a reason to refuse the plugin: a cosmetic field must not make it uninstallable
    css = pluginstyle.css_files(man.get("css")) if web and pluginstyle else []
    if not (web or run or themes or man.get("skills")):
        return None
    icon = man.get("icon")
    if icon is not None and not (_file(icon) and os.path.splitext(icon)[1].lower() in ICON_TYPES):
        return None
    # an icon, what it does and why: every plugin has them (owner decision 2026-10-03); if missing, the plugin installs but the catalog flags it
    missing = [k for k in ("icon", "description", "why") if not str(man.get(k) or "").strip()]
    return {"name": man["name"], "icon": icon, "missing": missing, "description": str(man.get("description") or "")[:300],
            "why": str(man.get("why") or "")[:500], "web": web, "run": run,
            "themes": list(themes or []), "css": css, "skills": bool(man.get("skills")),
            "hub": str(man["hub"]) if man.get("hub") else None, "version": str(man.get("version") or "")[:20],
            # page code is full access to the page (declared or not: there is no isolation); the only value is "full"
            "page_access": "full" if web else None}


def repo_of(text):
    """"<account>/<repo>" from a GitHub link (owner decision 2026-10-03: just paste a GitHub link to the plugin):
    https://github.com/a/r, github.com/a/r.git, …/tree/main/…, or a/r itself. Not GitHub or not a repo: None."""
    t = str(text or "").strip()
    m = re.fullmatch(r"(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?(?:[/#?].*)?", t) or \
        re.fullmatch(r"([^/\s]+)/([^/\s]+?)(?:\.git)?", t)
    if not m or not ACCOUNT.fullmatch(m.group(1)) or not re.fullmatch(r"[A-Za-z0-9._-]+", m.group(2)) \
            or m.group(2).strip(".") == "":
        return None
    return f"{m.group(1)}/{m.group(2)}"


def _manifest(path):
    try:
        with open(os.path.join(path, MANIFEST), encoding="utf-8") as f:
            return valid(json.load(f))
    except (OSError, ValueError):
        return None


def review_task(rec):
    """Hub Developer task: read the plugin code in the review clone and give a verdict."""
    full = rec["repo"]
    return (f"Review the code of plugin {full}@{rec['commit'][:7]} before it is installed — the owner asked to \"Review "
            f"and install\". Clone: {rec['path']} (nothing is connected, run nothing). Read all the code and the "
            "hub-plugin.json manifest: what the page part (web) and the process (run) do, where they go on the network, "
            "which files they read and write, whether they touch keys, the hub's settings and journal, whether they call "
            "hub and what for, skills (if skills). Does this match what the description promises. The result — by the "
            f"command: hub market verdict {full} ok|risky|bad \"<one or two sentences: what it does and what is risky>\" "
            "(ok — does what it promises, nothing extra; risky — there is something the owner should know about; bad — "
            "dangerous or not what it claims). To the owner — the same briefly, and what you found.")


def icon_url(full, icon, ref="HEAD"):
    """The plugin icon from the catalog, straight from the author's repo (the page shows it through <img>)."""
    return f"https://raw.githubusercontent.com/{full}/{ref}/{icon}"


def page_access(p):
    """"full" if the plugin has page code, otherwise None: page code is always full access to the page."""
    return "full" if p.get("web") else None


def kinds(man):
    return [i18n.t(k) for k, on in (("cli.market.kind_page", man.get("web")), ("cli.market.kind_process", man.get("run")),
                                    ("cli.market.kind_themes", man.get("themes")),
                                    ("cli.market.kind_skills", man.get("skills"))) if on]


def from_catalog(rec):
    """An install record from a connected catalog (core/mpinstall: market + entry) -> "<entry>@<catalog>" or None."""
    ok = isinstance(rec, dict) and all(isinstance(rec.get(k), str) and rec[k] for k in ("market", "entry"))
    return f"{rec['entry']}@{rec['market']}" if ok else None


def catalog_record(cfg, key):
    """Plugin key (an extension or a skill set) was installed from a catalog -> "<entry>@<catalog>" or None. It is updated and
    removed by core/mpinstall, exactly at the catalog commit, not by this path (author's head, own folder in market/)."""
    exts = cfg.get("extensions") if isinstance(cfg.get("extensions"), dict) else {}
    sets = cfg.get("sets") if isinstance(cfg.get("sets"), list) else []
    return from_catalog(exts.get(key) if isinstance(exts.get(key), dict) else next(
        (x for x in sets if isinstance(x, dict) and x.get("name") == key), None))


class Market:
    def __init__(self, cfg_path, store, api=github, clone_url=lambda full: f"https://github.com/{full}.git",
                 restart=None, events=None, via="cli", cats=None):
        self.cfg_path, self.store, self.api, self.clone_url = cfg_path, store, api, clone_url
        self.state_dir = os.path.dirname(os.path.abspath(store))   # store is <state>/market: catalogs sit next to it
        self._cats = cats   # readable catalogs (core/marketplaces.readable): once per this object
        self.restart = restart   # parts of hub-up -> restart (hub.restart_requested); None: only a hint
        self.events = events     # (type, data) -> a fact to the journal: consent to new rights (plugins.consented)
        self.via = via           # where the command came from: cli | hub | web, goes into the consent event
        self.newer = ""          # the last reviewed install's note that the author's head is another commit (_newer)

    def trusted(self):
        """Always empty: the hub's old list of plugins is gone (marketplace spec §4, S8). The page server of the previous
        release imports this module from the new files during the update's self-check and its catalog search still asks
        for the list (core.marketplaces in its memory)."""
        return []

    def cats(self):
        """Readable connected catalogs; each costs a `git show` of its commit, so read once per object (per request, per command)."""
        from core import marketplaces
        if self._cats is None:
            self._cats = marketplaces.readable(self.state_dir, self.load())
        return self._cats

    # --- settings ------------------------------------------------------------------------------------------------
    def load(self):
        return catalog.read_settings(self.cfg_path)

    def editing(self):
        """hub.json for editing: read and write under a lock (catalog.edit_settings). The page, `hub market` and
        `hub module` write it at the same time, and without the lock a late write lost another's edit. The lock is not reentrant: inside
        the block, neither editing nor edit_settings."""
        return catalog.edit_settings(self.cfg_path)

    def search(self, query=""):
        """Plugins sent to review by link, until installed: their verdict and "Install the reviewed one" are on the page.
        Everything else is in the catalogs (core/marketplaces.page)."""
        cfg = self.load()
        installed = {x.get("source") for x in list((cfg.get("extensions") or {}).values()) + list(cfg.get("sets") or [])}
        q, out = (query or "").lower().split(), []
        for full, rec in sorted(self.reviews().items()):
            man = None if full in installed else _manifest(rec.get("path") or "")
            text = " ".join([full, (man or {}).get("name") or "", (man or {}).get("description") or ""]).lower()
            if man and all(w in text for w in q):
                st, why = version.compat(man.get("hub"))
                out.append({**man, "repo": full, "kinds": kinds(man), "trusted": False, "link": True,
                            "page_access": page_access(man),
                            "icon_url": icon_url(full, man["icon"], rec["commit"]) if man.get("icon") else None,
                            "installed": False, "compat": st, "warning": why})
        return out

    # --- review before install (owner decision 2026-10-03: the hub looks at the plugin code itself and connects it only
    # after that, with the user's consent): a clone at a commit in the review folder, a Hub Developer verdict, install of that clone ------
    @staticmethod
    def _outside(full):
        """Outside the connected catalogs the author's commit is pinned by nothing: only through code review."""
        return Error(i18n.t("cli.market.outside", repo=full))

    def _catalog(self, label, accept):
        """A connected catalog entry is installed by core/mpinstall, exactly at the catalog commit, into the new storage (marketplace
        spec §4: the old commands are synonyms). Its refusals are the market's refusals (the page and the CLI already catch them)."""
        from core import mpinstall
        try:
            return "\n".join(mpinstall.install(self.state_dir, self.cfg_path, label, accept=accept, via=self.via,
                                               events=self.events, restart=self.restart))
        except mpinstall.NeedsConsent as e:
            raise NeedsConsent(str(e), e.wider) from None
        except RuntimeError as e:
            raise Error(str(e)) from None

    def _review_file(self, full):
        return os.path.join(self.store, "_review", *full.split("/")) + ".json"

    def reviews(self):
        """{repo: {commit, state: checking|ok|risky|bad, summary, path, ts}}: plugin reviews."""
        out, root = {}, os.path.join(self.store, "_review")
        for acc in os.listdir(root) if os.path.isdir(root) else []:
            if not os.path.isdir(os.path.join(root, acc)):   # .DS_Store and the like next to account folders
                continue
            for f in os.listdir(os.path.join(root, acc)):
                if f.endswith(".json"):
                    try:
                        with open(os.path.join(root, acc, f), encoding="utf-8") as fh:
                            rec = json.load(fh)
                        out[rec["repo"]] = rec
                    except (OSError, ValueError, KeyError):
                        pass
        return out

    def _save_review(self, rec):
        path = self._review_file(rec["repo"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)

    def stage(self, link, to=None):
        """A plugin clone at a commit in the review folder: the code is connected nowhere, Hub Developer reads it. Any
        public GitHub repo, by link (owner decision 2026-10-03: only through review): no need to connect a marketplace,
        only the reviewed clone is installed afterwards."""
        import time
        full = repo_of(link)
        if not full:
            raise Error(i18n.t("cli.market.need_link"))
        account, _, repo = full.partition("/")
        path = os.path.join(self.store, "_review", account, repo)
        platforms.current.rmtree(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", self.clone_url(full), path], check=True,
                       capture_output=True, text=True)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True,
                                text=True).stdout.strip()
        man = _manifest(path)
        if not man:
            platforms.current.rmtree(path)
            raise Error(i18n.t("cli.market.not_plugin", repo=full, manifest=MANIFEST))
        rec = {"repo": full, "name": man["name"], "commit": commit, "path": path, "state": "checking", "summary": "",
               "ts": time.time(), **({"to": to} if to else {})}   # to: who the review went to; only that one writes the verdict
        self._save_review(rec)
        return rec

    def staged(self, rec, dev):
        """The answer of `hub market review`: how the reviewed one goes in. A plugin installed by this link is updated by
        its key (`hub market update <key> --reviewed`: on the owner's word it needs no naming of the source)."""
        cfg = self.load()
        rows = list((cfg.get("extensions") or {}).items()) + [   # skills only: a set under its key, not a whole set
            (x["name"], x) for x in cfg.get("sets") or [] if isinstance(x, dict) and ":" in str(x.get("name") or "")]
        key = next((k for k, x in rows if isinstance(x, dict) and not x.get("subdir")
                    and not catalog_record(cfg, k) and str(x.get("source") or "").lower() == rec["repo"].lower()), None)
        args = {"repo": rec["repo"], "commit": rec["commit"][:7], "dev": dev}
        return i18n.t("cli.market.review_staged_update", name=key, **args) if key else \
            i18n.t("cli.market.review_staged", **args)

    def verdict(self, full, verdict, summary):
        import time
        if verdict not in ("ok", "risky", "bad"):
            raise Error(i18n.t("cli.market.bad_verdict"))
        rec = self.reviews().get(full)
        if not rec:
            raise Error(i18n.t("cli.market.not_staged", repo=full))
        rec.update(state=verdict, summary=str(summary or "")[:600], checked=time.time())
        self._save_review(rec)
        return f"{full}@{rec['commit'][:7]}: {verdict}"

    def _head(self, full):
        """The author's HEAD commit by git ls-remote, or None when the network does not answer (offline, a timeout). No
        prompt and no sign-in window: a repo gone private must not ask for a password in the owner's terminal."""
        from core import codestore
        try:
            return subprocess.run(["git", "ls-remote", self.clone_url(full), "HEAD"], check=True, capture_output=True,
                                  text=True, timeout=20, stdin=subprocess.DEVNULL,
                                  env=codestore.quiet_env(), **codestore.git_cwd()).stdout.split()[0]
        except (subprocess.SubprocessError, OSError, IndexError):
            return None

    def _newer(self, full, commit):
        """A note for the answer when the author's head is not the reviewed commit: "install --reviewed" puts exactly the
        reviewed one, and must not stay silent that something newer exists and was not looked at. Network down: no note."""
        head = self._head(full)
        if not head or head == commit:
            return ""
        return i18n.t("cli.market.newer_unreviewed", commit=head[:7], repo=full)

    # --- install -------------------------------------------------------------------------------------------------
    def install(self, full, reviewed=False, accept=False):
        from core import marketplaces
        account, _, repo = (full or "").partition("/")
        cfg = self.load()
        label = None if reviewed else marketplaces.entry_for_repo(self.state_dir, cfg, full, self.cats())
        if label:   # the repo is pinned in a connected catalog: the catalog installs it, not the author's head
            return self._catalog(label, accept)
        if not reviewed:
            raise self._outside(full)
        # exactly the reviewed clone is installed: the author's new commit after the review does not get here
        rec = self.reviews().get(full)
        if not rec or rec.get("state") not in ("ok", "risky"):
            raise Error(i18n.t("cli.market.review_open", repo=full,
                               state=(rec or {}).get("state") or i18n.t("cli.market.not_reviewed")))
        man = _manifest(rec["path"])   # the manifest is from the review clone
        if not man:
            raise Error(i18n.t("cli.market.no_manifest", repo=full, manifest=MANIFEST))
        exts = cfg.setdefault("extensions", {})
        key = f"{account.lower()}:{man['name']}"
        if key in exts and exts[key].get("source") != full:
            raise Error(i18n.t("cli.market.same_name", name=key, source=exts[key].get("source")))
        path = os.path.join(self.store, account, repo)
        # the new copy is downloaded and checked alongside, and replaces the installed one only whole (2026-10-19: git
        # broke off in the middle of update and three plugins were left without a folder)
        new = path + NEW
        platforms.current.rmtree(new)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            # exactly what was committed at the reviewed commit: edits to working files after the verdict do not travel here
            subprocess.run(["git", "clone", "-q", "--no-checkout", rec["path"], new], check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", rec["commit"]],
                           cwd=new, check=True, capture_output=True, text=True)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=new, check=True, capture_output=True,
                                    text=True).stdout.strip()
            if commit != rec["commit"]:
                raise Error(i18n.t("cli.market.clone_changed", repo=full, commit=commit[:7], reviewed=rec["commit"][:7]))
            try:
                with open(os.path.join(new, MANIFEST), encoding="utf-8") as f:
                    man = valid(json.load(f))  # install what is in the clone
            except (OSError, ValueError):
                man = None
            if not man:
                raise Error(i18n.t("cli.market.no_manifest", repo=full, manifest=MANIFEST))
            key = f"{account.lower()}:{man['name']}"
            st, why = version.compat(man["hub"])
            if st == "incompatible":
                raise Error(f"{full}: {why}")
            rights = plugrights.of(man, new)
            # update: the new version's rights against what was approved for the installed one; first install is the command itself
            mine = [x for x in exts.values() if isinstance(x, dict) and x.get("source") == full]
            sets = [s for s in cfg.get("sets") or [] if isinstance(s, dict) and s.get("source") == full]
            wider = []
            if mine or sets:
                wider = plugrights.wider(rights, plugrights.approved_of(mine[0] if mine else None, sets)[0])
            if wider and not accept:
                raise NeedsConsent(consent_text(key, commit, wider, f"hub market update {key}"), wider)
        except BaseException:
            platforms.current.rmtree(new)
            raise
        stopped = []   # the plugin switched off for the swap: only when the folder turned out to be held
        try:
            _swap(new, path, stop=lambda: stopped.extend(self._stop(full)) or bool(stopped))
        except BaseException:
            self._resume(stopped, restart=True)
            raise
        self._resume(stopped)   # the flag off before the record below is made; the process starts at its end
        notes = [why] if why else []
        # the record goes into hub.json as it became after the clone: the clone took long, the page could have written its own
        with self.editing() as cfg:
            exts = cfg.setdefault("extensions", {})
            # the owner turned it off (and before the rename in the manifest): do not turn it on
            off = any(x.get("disabled") for x in exts.values() if x.get("source") == full)
            # the owner's own settings in the record (folder etc.) survive an update; what the market manages comes from the manifest
            own = {k: v for x in exts.values() if x.get("source") == full for k, v in x.items() if k not in MANAGED}
            if man["web"] or man["run"] or man["themes"]:
                exts[key] = {**own, "path": path, "source": full, "commit": commit,
                             **{k: man[k] for k in FROM_MANIFEST if man[k]},
                             **({"disabled": True} if off else {}), "approved": rights}
                if man["themes"]:
                    notes.append(i18n.t("cli.market.note_themes"))
                if man["web"]:
                    notes.append(i18n.t("cli.market.note_page_access"))
                    notes.append(i18n.t("cli.market.note_reload"))
            if man["skills"]:
                sets = [s for s in cfg.get("sets") or [] if s.get("source") != full]
                cfg["sets"] = sets + [{"name": key, "path": path, "source": full, "commit": commit, "approved": rights}]
                notes.append(i18n.t("cli.market.note_skills", name=key))
        if man["run"] and not off:   # after save: hub-up takes the plugin command from the settings
            if self.restart:
                self.restart([f"ext:{key}"])
                notes.append(i18n.t("cli.market.note_process_started", name=key))
            else:
                notes.append(i18n.t("cli.market.note_process_later", name=key))
        if wider:   # consent goes to the journal and to the owner in Telegram: the page could click it, and so could plugin code (§3.6)
            notes.append(i18n.t("cli.market.note_allowed", rights="; ".join(plugrights.shown(w) for w in wider)))
            if self.events:
                self.events("plugins.consented", {"key": key, "sha": commit, "source": full, "via": self.via,
                                                  "wider": [plugrights.shown(w) for w in wider]})
        self.newer = self._newer(full, commit)
        return i18n.t("cli.market.installed", name=key, repo=full, commit=commit[:7],
                      notes="; ".join(notes + [self.newer] if self.newer else notes))

    def _stop(self, full):
        """Windows: a running plugin process holds its folder and the swap fails (WinError 32). Switch the plugin off
        in the settings and ask the hub to restart its part: hub-up stops it and does not start it. Returns the keys it
        switched off (a plugin the owner had off is not touched), [] when there was nothing to stop."""
        if not self.restart:
            return []
        keys = []
        with self.editing() as cfg:
            for k, x in (cfg.get("extensions") or {}).items():
                if isinstance(x, dict) and x.get("source") == full and x.get("run") and not x.get("disabled"):
                    x["disabled"] = True
                    keys.append(k)
        if keys:
            try:
                self.restart([f"ext:{k}" for k in keys])
            except BaseException:   # the caller has not got the keys yet: take the flag back here, or it stays on for good
                self._resume(keys)
                raise
        return keys

    def _resume(self, keys, restart=False):
        """Take back the flag `_stop` set; restart: start the part again now (the swap failed, the old copy stays)."""
        if not keys:
            return
        with self.editing() as cfg:
            for k in keys:
                x = (cfg.get("extensions") or {}).get(k)
                if isinstance(x, dict):
                    x.pop("disabled", None)
        if restart and self.restart:
            self.restart([f"ext:{k}" for k in keys])

    def _find(self, name):
        """(settings, key, source): `<account>:<name>` or just `<name>` if there is only one such plugin."""
        cfg = self.load()
        mine = {k: x["source"] for k, x in (cfg.get("extensions") or {}).items() if x.get("source")}
        mine.update({x["name"]: x["source"] for x in cfg.get("sets") or [] if x.get("source")})
        keys = [name] if name in mine else [k for k in mine if k.split(":")[-1] == name]
        if len(keys) > 1:
            raise Error(i18n.t("cli.market.ambiguous", name=name, keys=", ".join(sorted(keys))))
        if not keys:
            raise Error(i18n.t("cli.market.not_installed", name=name))
        return cfg, keys[0], mine[keys[0]]

    def updates(self):
        """{key: {commit, version}}: installed from the marketplace, where the author has published something newer (owner decision
        2026-10-03: why is the new version not shown while everyone has an update button). The commit is from git ls-remote, the version is from
        the author's manifest; if the network did not answer, the plugin is skipped rather than alarmed about."""
        from core import marketplaces
        cfg, out = self.load(), {}
        cats = self.cats()   # once per selection
        for key, ext in (cfg.get("extensions") or {}).items():
            src, have = ext.get("source"), ext.get("commit") or ""
            # from a catalog (and an old record of the same source, §4): its version is in the catalog, not the author's head
            if not src or not have or marketplaces.entry_for(self.state_dir, cfg, key, cats):
                continue
            head = self._head(src)
            if not head or head == have:
                continue
            try:
                man = valid(self.api(f"https://api.github.com/repos/{src}/contents/{MANIFEST}")) or {}
            except Exception:  # noqa: BLE001 — no version: show the commit
                man = {}
            # unchecked: installs without a code review — never outside catalogs (the hub's old list is gone, S8); the field
            # stays for the page: without it, it offered "Update without review", which the core refuses
            out[key] = {"commit": head, "version": man.get("version") or "", "repo": src, "unchecked": False}
        return out

    def update(self, name, reviewed=False, accept=False):
        from core import marketplaces
        cfg, key, src = self._find(name)
        label = None if reviewed else marketplaces.entry_for(self.state_dir, cfg, key, self.cats())
        if label:   # from a catalog or its source is in a catalog (§4): the catalog version, into the new storage (M2)
            ext = (cfg.get("extensions") or {}).get(key) or {}
            if not ext.get("market"):   # before catalogs, from the author's head: may be newer than pinned, "update" will not roll back
                from core import mpinstall
                try:
                    have, e = ext.get("version"), mpinstall.find(self.state_dir, self.cfg_path, label)[3]
                except RuntimeError as err:
                    raise Error(str(err)) from None
                if marketplaces.version_order(e.get("version"), have) == -1:
                    raise Error(i18n.t("cli.market.newer_than_catalog", name=key, have=have, version=e["version"],
                                       label=label))
            return self._catalog(label, accept)
        rec = catalog_record(cfg, key)
        if rec:   # installed from a catalog exactly at its commit: the author's head bypassing the catalog is not installed
            raise Error(i18n.t("cli.market.from_catalog", name=key, label=rec))
        if not reviewed:   # outside catalogs the author's new commit is pinned by nothing
            raise Error(i18n.t("cli.market.update_outside", name=key, repo=src))
        path = os.path.join(self.store, *src.split("/"))
        # no folder (deleted by hand, an old failure): install again instead of failing on it (WinError 267)
        now = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True,
                             text=True).stdout.strip() if os.path.isdir(path) else ""
        # exactly the reviewed commit (the "Update with the reviewed one" button)
        self.newer = ""
        self.install(src, reviewed=True, accept=accept)
        head = (self.reviews().get(src) or {}).get("commit", "")
        return i18n.t("cli.market.updated_reviewed", name=key, old=now[:7], new=head[:7]) + (
            "; " + self.newer if self.newer else "")

    def remove(self, name):
        cfg, key, src = self._find(name)
        if catalog_record(cfg, key):   # its folder is in the catalog storage: the catalog installer removes it
            from core import mpinstall
            try:
                return "\n".join(mpinstall.uninstall(self.state_dir, self.cfg_path, key, events=self.events,
                                                     restart=self.restart))
            except RuntimeError as e:
                raise Error(str(e)) from None
        ran = False
        with self.editing() as cfg:
            exts = cfg.get("extensions") or {}
            if (exts.get(key) or {}).get("source") == src:
                ran = bool(exts.pop(key).get("run"))
            cfg["sets"] = [s for s in cfg.get("sets") or [] if s.get("source") != src]
        # the plugin process is stopped by hub-up: the part is no longer in the settings (otherwise it lived until the hub restart and held the port, 2026-10-20)
        if ran and self.restart:
            self.restart([f"ext:{key}"])
        platforms.current.rmtree(os.path.join(self.store, *src.split("/")))
        return i18n.t("cli.market.removed", name=key)


NEW, OLD = ".installing", ".previous"   # next to the plugin folder: the new copy before the swap and the old one during the swap


# with a half-second pause: the stopped plugin process takes a moment to exit and let go of its folder; hub-up gives it
# stop_grace before it kills it, plus the hubd tick and the hub-up loop that carry the request (review)
SWAP_TRIES = 2 * (platforms.current.stop_grace(False) + 15)


def _swap(new, path, stop=None):
    """A ready copy `new` goes in place of `path`: the old one is set aside and deleted only when the new one is in place. If it did not
    work (Windows: the plugin process holds the folder), stop() is called once: it switches the plugin off and asks the hub
    to stop the process (true: it did); then SWAP_TRIES half-second retries. Still held: the installed one stays as it was."""
    old = path + OLD
    platforms.current.rmtree(old)
    if os.path.isdir(path):
        tries, asked = 1, False
        while True:
            try:
                os.rename(path, old)
                break
            except OSError as e:
                if not asked and stop:
                    asked = True
                    try:
                        held = stop()
                    except BaseException:   # the request to stop failed: the downloaded copy does not stay (review)
                        platforms.current.rmtree(new)
                        raise
                    if held:
                        tries = SWAP_TRIES
                tries -= 1
                if tries > 0:
                    time.sleep(0.5)
                    continue
                platforms.current.rmtree(new)
                raise Error(i18n.t("cli.market.folder_busy", path=path, error=e)) from e
    try:
        os.rename(new, path)
    except OSError:
        if os.path.isdir(old):
            os.rename(old, path)
        platforms.current.rmtree(new)
        raise
    platforms.current.rmtree(old)


INSTEAD = {"list", "search", "install", "update", "remove"}   # review and verdict have no replacement yet


def _instead(m, cmd, rest):
    """The `hub marketplace` command to type instead of `hub market <cmd> …`, or None: this plugin has none
    (outside catalogs: the old path)."""
    from core import marketplaces
    arg = next((x for x in rest if not x.startswith("--")), "")
    if cmd in ("search", "list"):
        return f"hub marketplace {cmd}" + (f" {' '.join(rest)}" if rest else "")
    cfg = m.load()
    try:
        key = m._find(arg)[1] if cmd in ("update", "remove") else None
    except Error:
        return None
    if cmd == "remove":
        return f"hub marketplace uninstall {key}"
    label = marketplaces.entry_for_repo(m.state_dir, cfg, arg, m.cats()) if cmd == "install" else \
        marketplaces.entry_for(m.state_dir, cfg, key, m.cats())
    return f"hub marketplace install {label}" if label and "--reviewed" not in rest else None


def main(argv, restart=None, events=None):
    """hub market …: restart(parts) and events(kind, data) are the restart of parts and facts (consent to new rights) of whoever
    called (bin/hub put: its channel and sender)."""
    from core.daemon import default_state_dir
    state = default_state_dir()
    m = Market(catalog.config_path(state), os.path.join(state, "market"), restart=restart, events=events,
               via="hub" if os.environ.get("HUB_SESSION") else "cli")
    cmd, rest = (argv[0] if argv else "list"), argv[1:]
    from core import marketplaces
    try:
        if cmd == "add" or cmd == "list" and rest[:1] and rest[0] not in marketplaces._catalogs(m.load()):
            # a GitHub account as a marketplace (`markets`) is gone (S8); a flag is not an account: the usage
            print(__doc__ if rest[:1] and rest[0].startswith("-") else i18n.t("cli.market.account_gone"))
            return 2
        if cmd in INSTEAD:   # synonyms for one epoch (marketplace spec §3.2, version.DEPRECATED): what to type now
            now = _instead(m, cmd, rest)
            print(f"! {version.deprecated(['hub market'])[0]}" + (i18n.t("cli.market.now", command=now) if now else ""))
        if cmd == "list" and len(rest) <= 1:
            print("\n".join(marketplaces.listing(state, m.cfg_path, rest[0] if rest else None)))
        elif cmd == "verdict" and len(rest) >= 3:  # Hub Developer after reading the plugin code
            print(m.verdict(rest[0], rest[1], " ".join(rest[2:])))
        elif cmd == "search":   # catalogs, same as hub marketplace search; plus plugins by link under review
            more = m.search(" ".join(rest))
            found = marketplaces.search(state, m.cfg_path, rest)
            if not (more and found == [marketplaces.NOTHING]):   # "nothing found" above what was found by link
                print("\n".join(found))
            for p in more:
                warn = f"  ⚠ {p['warning']}" if p.get("warning") else ""
                print(f"  {p['repo']:<36} {p['name']:<16} {', '.join(p['kinds']):<22} "
                      f"{i18n.t('cli.market.by_link')} "
                      f"{i18n.t('cli.market.installed_mark') if p['installed'] else ''} "
                      f"{p.get('description', '')[:70]}{warn}")
        elif cmd in ("install", "update") and len([x for x in rest if not x.startswith("--")]) == 1 \
                and {x for x in rest if x.startswith("--")} <= {"--reviewed", "--accept"}:
            # --reviewed: exactly the reviewed clone; --accept: the owner allowed the new rights the update asked about
            arg = next(x for x in rest if not x.startswith("--"))
            reviewed, accept = "--reviewed" in rest, "--accept" in rest
            if cmd == "install":
                print(m.install((repo_of(arg) or "") if reviewed else arg, reviewed=reviewed, accept=accept))
            else:
                print(m.update(arg, reviewed=reviewed, accept=accept))
        elif cmd == "remove" and len(rest) == 1:
            print(m.remove(rest[0]))
        else:
            print(__doc__)
            return 2
    except (Error, RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as e:   # ValueError: a broken hub.json
        print(i18n.t("cli.market.not_done", error=getattr(e, "stderr", "") or e).strip())
        return 1
    return 0
