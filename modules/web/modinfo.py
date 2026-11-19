"""Hub modules for "Settings" → "Modules": browsers, Tailscale and the other optional modules are all listed there with
a detailed description of what they give. What a module gives, whether it is on now (from facts: settings, state files,
the core snapshot) and how to turn it on."""
import json
import os
import re
import shutil
import subprocess

from core import catalog, cutout, i18n

ON, OFF, PART = "on", "off", "part"   # working · off / not configured · partly working


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


SECRETISH = re.compile(r"(?<![\w/.-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{24,}")


def masked(cmd):
    """A command for display on the page: anything that looks like a key (24+ letters and digits outside a path) is hidden."""
    return SECRETISH.sub("•••", cmd or "")


def modules(state_dir, cfg, snap, lang=None):
    """[{id, title, gives, state, detail, how, section?, toggle?, core?}] — core: the hub's own part, always on, no switch;
    section: the page embeds its own settings section;
    toggle: the card has a switch (POST /api/modules/toggle, the TOGGLES section in hub.json). The texts are in the
    language lang (without it, the process language, core/i18n.py current)."""
    lang = lang or i18n.current()
    t = lambda key, **p: i18n.tr(lang, key, **p)   # noqa: E731
    out = []

    def add(mid, title, gives, state, detail="", how="", section=None, command=None, toggle=False, enabled=None, core=False):
        out.append({"id": mid, "title": title, "gives": gives, "state": state, "detail": detail, "how": how,
                    **({"section": section} if section else {}), **({"command": command} if command else {}),
                    **({"core": True} if core else {}),
                    **({"toggle": True, "enabled": state != OFF if enabled is None else enabled} if toggle else {})})

    tg = _read(os.environ.get("HUB_TELEGRAM_CONFIG") or os.path.join(catalog.config_dir(), "telegram.json"))
    add("telegram", "Telegram", t("web.modinfo.telegram_gives"),
        ON if tg.get("token") and (tg.get("owner") or tg.get("owners")) else OFF,
        t("web.modinfo.telegram_detail") if tg.get("token") else "", t("web.modinfo.telegram_how_file"))

    from modules.tailnet import access
    ts = access.status(state_dir)   # a record the tailnet part stopped rewriting reads as not working, with why
    tcfg = cfg.get("tailnet") if isinstance(cfg.get("tailnet"), dict) else {}
    # off until tailnet.enabled is true (modules/tailnet/run.py); the switch shows on while it works — the first
    # minute after an update from a release that turned it on by itself, before its tick writes the key
    enabled = tcfg.get("enabled") is True or ("enabled" not in tcfg and bool(ts.get("ok") or ts.get("stale")))
    on = bool(ts.get("ok")) and enabled   # just turned off: off at once, before the module rewrites its record
    add("tailnet", t("web.modinfo.tailnet_title"), t("web.modinfo.tailnet_gives"),
        ON if on else OFF,
        "  ".join(f"{k}: {v}" for k, v in (ts.get("urls") or {}).items()) if on
        else "" if ts.get("off") or not enabled else (ts.get("why") or ""),   # off: "How to enable" below says it
        t("web.modinfo.tailnet_how"), toggle=True, enabled=enabled)

    from modules.watch import watch
    wcfg = watch.config()
    add("watch", t("web.modinfo.watch_title"), t("web.modinfo.watch_gives"), OFF if wcfg.get("off") else ON, "", t("web.modinfo.watch_how"),
        toggle=True)

    add("surrender", t("web.modinfo.surrender_title"), t("web.modinfo.surrender_gives"), ON if wcfg.get("surrender") else OFF, "",
        t("web.modinfo.surrender_how"), toggle=True)

    # without the watcher's model ({"off": true}) there are no reminders, even though the option is on
    add("promise", t("web.modinfo.promise_title"), t("web.modinfo.promise_gives"),
        (PART if wcfg.get("off") else ON) if wcfg.get("promise") is True else OFF,
        t("web.modinfo.promise_watch_off") if wcfg.get("promise") is True and wcfg.get("off") else "",
        t("web.modinfo.promise_how"), toggle=True)

    br = snap.get("browsers") or []
    # idle: not running, it starts when a session calls it; a problem with either (its MCP program is missing): not working
    up = [b for b in br if (b.get("up") or b.get("idle")) and not b.get("problem")]
    add("browsers", t("web.modinfo.browsers_title"), t("web.modinfo.browsers_gives"),
        ON if br and len(up) == len(br) else PART if up else OFF,
        ", ".join(f"{b['name']}: " + (t("web.modinfo.browser_up") if b.get("up") else
                                      t("web.modinfo.browser_idle") if b.get("idle") else t("web.modinfo.browser_down"))
                  for b in br),
        t("web.modinfo.browsers_how"), section="browsers")

    from modules.proof import video
    ff = video.ffmpeg(cfg)
    add("video", t("web.modinfo.video_title"), t("web.modinfo.video_gives"),
        (ON if ff else PART) if video.enabled(cfg) else OFF,
        t("web.modinfo.video_no_ffmpeg") if video.enabled(cfg) and not ff else "",
        t("web.modinfo.video_how"), toggle=True, enabled=video.enabled(cfg))

    from modules.critic import critic
    cs = critic.settings(cfg)
    add("critic", t("web.modinfo.critic_title"), t("web.modinfo.critic_gives"),
        ON if critic.enabled(cfg) else OFF,
        t("web.modinfo.critic_detail", engine=cs["engine"], model=cs["model"] if cs["engine"] == "codex" else cs["claude_model"],
          fallback=cs["claude_model"]) if critic.enabled(cfg) else "",
        t("web.modinfo.critic_how_card"), toggle=True, enabled=critic.enabled(cfg))

    v = cfg.get("voice") or {}
    from modules.voice import local, transcribe
    # the built-in model (modules/voice); a record whose files are gone counts too: recordings go to it (transcribe)
    built_in = None if transcribe.command(cfg) else \
        (local.installed(state_dir) or (local.read_record(state_dir) if hasattr(local, "read_record") else None))
    # not transcribe.enabled(state=): a page server of the previous release keeps the old transcribe and local in memory
    # while this file is read from disk (tests/test_old_page_server) — hence hasattr for what local.py got later
    add("voice", t("web.modinfo.voice_title"), t("web.modinfo.voice_gives"),
        ON if transcribe.command(cfg) or built_in else OFF, (built_in or {}).get("name") or v.get("model") or "",
        t("web.modinfo.voice_how"), section="voice",
        command=masked(transcribe.command(cfg)))  # the command that does the transcription — display only

    # the reply voice: the switch is the voice module's too (voice_reply.enabled); the open flavor is cut without the
    # built-in voice (core/cutout.py) — then the card has the switch and no details of the built-in one
    from modules.voice import speech
    own = cutout.present("speech")
    if own:
        from modules.voice import speak
        vs = speak.settings(cfg)
    on = speech.enabled(cfg)
    lack = speak.missing(cfg, lang) if on and own else []
    from core import providers
    if on and not own and not speech.active(cfg, providers.current(state_dir)):   # hubd's copy of who leads it
        lack.append(t("core.providers.none_speech"))   # no built-in voice and no voice module leads: text only
    if on and not (tg.get("token") and (tg.get("owner") or tg.get("owners"))):
        lack.append(t("web.modinfo.voice_reply_no_telegram"))
    add("voice_reply", t("web.modinfo.voice_reply_title"), t("web.modinfo.voice_reply_gives"),
        (PART if lack else ON) if on else OFF,
        ", ".join(lack) if lack else t("web.modinfo.voice_reply_detail", voice=vs["voice"], model=vs["version"])
        if on and own else "",
        t("web.modinfo.voice_reply_how"), toggle=True)

    from modules.jira import links
    j = links.settings(cfg)
    add("jira", "Jira", t("web.modinfo.jira_gives"),
        ON if j else OFF, f"{j['url']} · {', '.join(j['projects'])}" if j else "", t("web.modinfo.jira_how"))

    from modules.testchrome import chrome
    tc = chrome.configured(cfg)
    add("testchrome", t("web.modinfo.testchrome_title"), t("web.modinfo.testchrome_gives"),
        ON if tc else OFF, tc or (t("web.modinfo.testchrome_shell", path=chrome.shell()) if chrome.shell()
                                  else t("web.modinfo.testchrome_testing", path=chrome.testing_build()) if chrome.testing_build()
                                  else t("web.modinfo.testchrome_system", path=chrome.system()) if not chrome.MAC and chrome.system()
                                  else t("web.modinfo.testchrome_none")),
        t("web.modinfo.testchrome_how"))

    if cutout.present("stats"):
        from modules.stats import stats
        sc = stats.settings(cfg)
        add("stats", t("web.modinfo.stats_title"), t("web.modinfo.stats_gives"),
            ON if sc["enabled"] else OFF, sc["url"] if sc["enabled"] else "",
            t("web.modinfo.stats_how"), section="stats", toggle=True)

    add("files", t("web.modinfo.files_title"), t("web.modinfo.files_gives"), ON, t("web.modinfo.files_detail"), "", core=True)

    add("scheduler", t("web.modinfo.scheduler_title"), t("web.modinfo.scheduler_gives"), ON,
        t("web.modinfo.scheduler_detail", count=len([j for j in snap.get("schedule") or [] if j.get("state") == "active"])), "", core=True)

    up_cfg = cfg.get("update") or {}
    remote = ""
    if shutil.which("git"):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            remote = subprocess.run(["git", "remote"], cwd=repo, capture_output=True, text=True, timeout=5).stdout.split()
        except (OSError, subprocess.SubprocessError):
            remote = []
    # a hub from the install line updates by packages (update.app): its code folder is a layer without a repository
    has_remote = isinstance(up_cfg.get("app"), dict) or up_cfg.get("remote", "origin") in (remote or [])
    add("update", t("web.modinfo.update_title"), t("web.modinfo.update_gives"),
        ON if up_cfg.get("enabled") and has_remote else OFF,
        "" if has_remote else t("web.modinfo.update_detail"),
        t("web.modinfo.update_how_card"), toggle=True, enabled=bool(up_cfg.get("enabled")))

    add("directions", t("web.modinfo.directions_title"), t("web.modinfo.directions_gives"), ON, t("web.modinfo.directions_detail_builtin"),
        t("web.modinfo.directions_how"), core=True)

    from modules.contacts import contacts as book   # the one reader of the book (spec hubbus §5.5.1): both forms, odd entries skipped
    people = book.people(_read(os.environ.get("HUB_CONTACTS") or os.path.join(catalog.config_dir(), "contacts.json")))
    add("contacts", t("web.modinfo.contacts_title"), t("web.modinfo.contacts_gives"), ON, t("web.modinfo.contacts_detail", count=len(people)), "", core=True)

    add("agents", t("web.modinfo.agents_title"), t("web.modinfo.agents_gives"), ON, "Hub Developer, Agent Developer", "", core=True)

    add("coach", t("web.modinfo.coach_title"), t("web.modinfo.coach_gives"), ON, "", "hub coach", core=True)

    from modules.secrets import engine as secrets
    from modules.secrets import vault   # the hub's own vault (spec d2345345): on unless secrets.enabled is false
    try:
        kept = len(vault.items(state_dir))
    except vault.Error:
        kept = 0
    add("secrets", t("web.modinfo.secrets_title"), t("web.modinfo.secrets_gives"),
        ON if secrets.enabled(cfg) else OFF, t("web.modinfo.secrets_detail", count=kept) if secrets.enabled(cfg) else "",
        t("web.modinfo.secrets_how"), toggle=True, enabled=secrets.enabled(cfg))

    from modules.web import push
    pc = cfg.get("push") if isinstance(cfg.get("push"), dict) else {}
    on = pc.get("enabled") is True
    keyed = all(pc.get(k) for k in ("key_file", "key_id", "team_id", "topic"))
    add("push", t("web.modinfo.push_title"), t("web.modinfo.push_gives_apns"),
        (ON if keyed else PART) if on else OFF,
        (t("web.modinfo.push_no_apns_key") if not keyed else t("web.modinfo.push_detail", count=push.device_count(state_dir))) if on else "",
        t("web.modinfo.push_how_apns"), toggle=True, enabled=on)

    from modules.web.settings import reactions_on
    add("reactions", t("web.modinfo.reactions_title"), t("web.modinfo.reactions_gives"), ON if reactions_on(cfg) else OFF, "",
        t("web.modinfo.reactions_how"), toggle=True)

    from modules.web.server import live_extension   # the mark of a removed preinstalled plugin is not a plugin
    plugins = [k for k, e in (cfg.get("extensions") or {}).items() if live_extension(k, e)]
    add("plugins", t("web.modinfo.plugins_title"), t("web.modinfo.plugins_gives"),
        ON if plugins else OFF, t("web.modinfo.plugins_detail", count=len(plugins)), t("web.modinfo.plugins_how"))
    out.sort(key=lambda m: m["state"] != OFF)   # modules that are not on come first; the order within each group is unchanged (the sort is stable)
    return out


# module → its switch in the allowlist in modules/web/settings.py (modules are turned on from the modules page)
TOGGLES = {"voice_reply": "voice_reply.enabled", "stats": "stats.enabled", "critic": "critic.enabled",
           "video": "video.enabled", "update": "update.enabled", "secrets": "secrets.enabled", "push": "push.enabled", "reactions": "reactions.enabled", "tailnet": "tailnet.enabled", "watch": "watch.off", "surrender": "watch.surrender", "promise": "watch.promise"}


def installed(state_dir, cfg, st, now=None):
    """Module API modules installed with `hub module add` (a folder or git), and an embed's (core/embed.py: the build's
    code, on by their manifest) are third-party code with the hub's
    privileges, kept apart from the core parts above and marked as third-party. The same rows as `hub modules`
    (core/modules.rows), plus what the buttons need: whether it is in modules.enabled, whether a pause can be lifted,
    whether it wrote to the core state. st is the core state (modules, catalog, core_written)."""
    import time
    from core import corewrite, modrun
    from core import modules as mods
    now = time.time() if now is None else now
    recs, enabled = st.get("modules") or {}, set(mods.settings(cfg).get("enabled") or [])
    model = set(mods.settings(cfg).get("model") or [])   # who the owner allowed the model to (core/modelcall.py)
    marks = corewrite.marks(st, cfg)
    out = []
    for r in mods.rows(cfg, state_dir, st, now):
        if r["origin"] == "builtin" and not r.get("embed"):   # an embed's module is the build's code: shown and switched
            continue
        rec = recs.get(r["name"]) or {}
        last = rec.get("last") or {}
        caps = [str(c) for c in r["capabilities"]]
        out.append({"name": r["name"], "version": r["version"], "origin": r["origin"],
                    "source": r.get("source") if r["origin"] == "git" else r["dir"],
                    "commit": r.get("commit") or "", "ref": r.get("ref") or "", "trusted": bool(r.get("trusted")),
                    "api": r["api"], "description": r["description"], "state": r["state"],
                    # the reason in the core's own text (as in hub modules): a block is visible only this way
                    "reason": r["reason"] if r["state"] == "blocked" else "",
                    "until": rec.get("until") if r["state"] == "suspended" else None,
                    "failure": f"{last.get('hook')}: {last.get('error')}" if last and r["state"] in ("suspended", "stopped") else "",
                    # an embed's module is on by its manifest (core/modules.embed_on), the rest by modules.enabled
                    "enabled": r["on"] if "on" in r else r["name"] in enabled, "dev": r["dev"], "capabilities": caps,
                    "embed": r.get("embed") or "",   # the embed whose module this is (its switch keeps modules.off)
                    "halt": modrun.status(rec, now),   # ok | suspended | error | blocked — what "reset" clears
                    "core_write": "core.write" in caps, "core_written": marks.get(r["name"]),
                    "model": "model" in caps, "model_allowed": r["name"] in model})
    return out
