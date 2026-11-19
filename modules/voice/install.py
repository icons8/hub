"""The built-in voice: download, switch and remove (specs d2109189, d2126783). Turned on by a person — «Settings →
Modules → Voice» (the setup step `voice`, modules/setup/job.py) or `hub setup voice on [<model>]`; nothing is downloaded
before that.

What comes: transcribe.cpp's native library for this platform and its ctypes wrapper of the same version
(tools/package/versions.json "transcribe-native"), the model a person chose among the rows of the hub's language — the
recommended one by default — and the speech detector (modules/voice/models.json, made by tools/voice_catalog.py) —
every file by its pinned address, sha256 and size through core/fetch. Laid out as

    <state>/runtime/voice/<engine version>/native/   the library (the archive's files, no links)
    <state>/runtime/voice/<engine version>/py/transcribe_cpp/   the wrapper's Python files
    <state>/runtime/voice-models/<file>              the model and the detector
    <state>/runtime/voice.json                       what is installed (modules/voice/local.py reads it)

and checked (local.py --check: loaded, 1 s of silence recognized in the record's language) before voice.json is
written. One model on disk: another one comes while the record still names the old one (voice keeps working), the
record changes in one os.replace after the check, and only then — under the recognition lock, so a recording that
began on the old model ends on it — the files no record names go. An engine already in place is not touched. Remove:
voice.json first (the voice is off from that moment), then the folders."""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

from modules.voice import local

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSIONS = os.path.join(REPO, "tools", "package", "versions.json")
MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
ENGINE = "transcribe-native"
LIBRARY = {"linux": "libtranscribe.so", "mac": "libtranscribe.dylib", "windows": "transcribe.dll"}
CHECK_WAIT = 300   # s: the check waits for the recognition lock (local.LOCK_WAIT) and loads a model of up to 2.5 GB
PRUNE_WAIT = 600   # s: the old model's files wait at most this long for a running recording
MAX_ENTRIES = 500
CLOUD = "chatgpt"   # the cloud row's id (modules/voice/chatgpt.ID): its files are named here too, for the open flavor's off
CLOUD_FILES = ("voice-cloud.json", "voice-cloud-last.json")


class Refused(Exception):
    """Voice cannot be turned on here or the download did not work out; `why` — a short code for the page."""

    def __init__(self, text, why):
        super().__init__(text)
        self.why = why


def _pin(got):
    if not isinstance(got, dict):
        return None
    url, sha, size = got.get("url"), got.get("sha256"), got.get("size")
    if isinstance(url, str) and url.startswith("https://") and isinstance(sha, str) and \
            re.fullmatch(r"[0-9a-f]{64}", sha) and type(size) is int and size > 0:
        return {"url": url, "sha256": sha, "size": size}
    return None


def engine_pin(plat=None, path=VERSIONS):
    """{version, lib: pin, wrapper: pin, platform} of the engine for this platform, or None (no build for it)."""
    import platform
    from modules.setup import chrome
    # the library is loaded into this Python: its own architecture (an x86_64 Python under Rosetta needs the x64
    # build, where chrome.key() names the Mac's own arm64 for a separate Chrome program)
    plat = plat or chrome.key(machine=platform.machine())
    try:
        with open(path, encoding="utf-8") as f:
            item = json.load(f).get(ENGINE)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(item, dict) or not plat:
        return None
    lib, wrapper = _pin((item.get("files") or {}).get(plat)), _pin(item.get("wrapper"))
    version = str(item.get("version") or "")
    if not (lib and wrapper and re.fullmatch(r"[0-9][0-9.]*", version)):
        return None
    return {"version": version, "lib": lib, "wrapper": wrapper, "platform": plat}


def base(lang):
    """A hub language's code as the catalog keys it: pt-BR → pt, zh-CN → zh."""
    return str(lang or "").replace("_", "-").split("-")[0].lower()


_LOADED = {}   # path -> (mtime, size, data): the page asks once a second during a download


def _catalog(path=MODELS):
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
        if _LOADED.get(path, (None,))[0] != key:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            _LOADED[path] = (key, data if isinstance(data, dict) else {})
    except (OSError, ValueError):
        return {}
    return _LOADED[path][1]


def choices(lang, path=MODELS):
    """The rows of «Settings → Modules → Voice» for the hub language, the recommended first: [{id, role, name, url,
    sha256, size, file, author, license, file_license, card, langs, english, error, wait30, memory, language}] —
    `language` is the model's own code for it when the model does not tell languages itself, or tells them badly
    (lang_hint: Whisper), and knows several (it is passed to the model then); [] — no model for the language."""
    data, code = _catalog(path), base(lang)
    rows = []
    for item in ((data.get("pick") or {}).get(code) or []):
        if not (isinstance(item, list) and len(item) >= 2):
            continue
        m = (data.get("list") or {}).get(item[0])
        pin = _pin(m)
        if not pin or not re.fullmatch(r"[\w.-]+\.gguf", str(m.get("file"))):
            continue
        langs = [str(x) for x in m.get("languages") or []]
        bases = list(dict.fromkeys(base(x) for x in langs))   # en-US and en-GB are one language
        own = next((x for x in langs if base(x) == code), None)
        if own is None:
            continue
        rows.append({**pin, "id": str(item[0]), "role": str(item[1]), "name": str(m.get("name") or item[0]),
                     "file": m["file"], "author": m.get("author"), "license": m.get("license"),
                     "file_license": m.get("file_license"), "card": m.get("card"), "langs": len(bases),
                     "english": code != "en" and "en" in bases,
                     "error": (m.get("errors") or {}).get(code), "wait30": m.get("wait30"), "memory": m.get("memory"),
                     "language": own if (not m.get("detects") or m.get("lang_hint")) and len(bases) > 1 else None,
                     "lang": code})
    return rows


def cloud_row(lang, state):
    """The cloud row (modules/voice/chatgpt.py) for the hub language, or None: the open flavor has no such file, and a
    page without the state (a test) shows only the local rows."""
    from core import cutout
    if not (state and cutout.present("voice_cloud")):
        return None
    from modules.voice import chatgpt
    rec = local.read_record(state) or {}
    return chatgpt.row(base(lang), state, fallback=rec.get("name") if local.installed(state) else None)


def rows(lang, state=None, path=MODELS):
    """What «Settings → Modules → Voice» lists: the local rows of the hub language, then the cloud row where there is one."""
    out = choices(lang, path)
    cloud = cloud_row(lang, state) if out else None   # no local model for the language — no voice to fall back on
    return out + ([cloud] if cloud else [])


def cloud_on(state):
    """The cloud row is chosen here and its code is in this tree."""
    from core import cutout
    if not (state and cutout.present("voice_cloud")):
        return False
    from modules.voice import chatgpt
    return chatgpt.chosen(state)


def drop_cloud(state):
    """The cloud row is no longer chosen: a local row was (its model checked)."""
    try:
        os.remove(os.path.join(local.runtime(state), CLOUD_FILES[0]))
    except OSError:
        pass


def model_pin(lang, model=None, path=MODELS):
    """{model, vad} pins for the hub language — the named row of it or the recommended one — or None (no model for the
    language, or that model is not among its rows)."""
    rows = choices(lang, path)
    row = next((r for r in rows if r["id"] == model), None) if model else (rows[0] if rows else None)
    data = _catalog(path)
    vad = _pin(data.get("vad"))
    if not (row and vad and re.fullmatch(r"[\w.-]+\.gguf", str(data["vad"].get("file")))):
        return None
    return {"model": row, "vad": {**vad, "id": data["vad"].get("id"), "file": data["vad"]["file"]}}


def engine_ready(state, version, plat):
    """The engine of this version is unpacked in place (an earlier install): the library and the wrapper are there."""
    final = os.path.join(local.runtime(state), "voice", version)
    return os.path.isfile(os.path.join(final, "native", library_name(plat))) and \
        os.path.isfile(os.path.join(final, "py", "transcribe_cpp", "__init__.py"))


def has_ffmpeg(cfg):
    """Every recording goes through ffmpeg to the model (transcribe.py): the package brings one, a hub from git needs it."""
    from modules.voice import transcribe
    path = transcribe.ffmpeg(cfg or {})
    return bool(path and (os.path.isfile(path) or shutil.which(path)))


def _present(path, pin):
    return os.path.isfile(path) and os.path.getsize(path) == pin["size"]


def row_view(r):
    """A row as the page and `hub setup voice` show it."""
    return {k: r.get(k) for k in ("id", "role", "name", "size", "error", "wait30", "langs", "english", "license",
                                  "file_license", "memory", "author", "lang", "cloud", "note", "act")}


def offer(lang, plat=None, cfg=None, state=None):
    """What «Turn on voice» would download here: {name, lang, size, models} — size: the recommended model, with the
    engine and the detector when they are not in place yet (state) — or {why: lang | platform | ffmpeg}."""
    e, m = engine_pin(plat), model_pin(lang)
    if not m:
        return {"why": "lang"}
    if not e:
        return {"why": "platform"}
    if cfg is not None and not has_ffmpeg(cfg):
        return {"why": "ffmpeg"}
    extra = 0 if state and engine_ready(state, e["version"], e["platform"]) else e["lib"]["size"] + e["wrapper"]["size"]
    if not (state and _present(os.path.join(local.runtime(state), "voice-models", m["vad"]["file"]), m["vad"])):
        extra += m["vad"]["size"]
    return {"name": m["model"]["name"], "lang": m["model"]["lang"], "size": m["model"]["size"] + extra, "extra": extra,
            "models": [row_view(r) for r in rows(lang, state)]}


def _member(name, top):
    """A tar entry's path below the archive's one top folder, or None (another top folder, `..`, absolute)."""
    parts = name.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0] != top or any(p in ("", "..") for p in parts[1:]) or re.match(r"[A-Za-z]:", name):
        return None
    return "/".join(parts[1:])


def unpack_native(archive, dest):
    """The engine's archive into dest: its regular files only (links, devices and paths outside are refused)."""
    with tarfile.open(archive, "r:gz") as t:
        members = t.getmembers()
        if len(members) > MAX_ENTRIES or not members:
            raise Refused(f"{len(members)} entries", "unpack")
        top = members[0].name.replace("\\", "/").split("/")[0]
        for m in members:
            rel = _member(m.name, top)
            if m.isdir() or (rel is None and m.name.rstrip("/") == top):
                continue
            if rel is None or not m.isfile():
                raise Refused(m.name, "unpack")
            path = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with t.extractfile(m) as src, open(path, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def unpack_wrapper(wheel, dest):
    """The wrapper's Python files (transcribe_cpp/*.py, py.typed) from its wheel into dest."""
    with zipfile.ZipFile(wheel) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"transcribe_cpp/[\w]+\.py|transcribe_cpp/py\.typed", n)]
        if "transcribe_cpp/__init__.py" not in names:
            raise Refused("no transcribe_cpp in the wheel", "unpack")
        for n in names:
            path = os.path.join(dest, *n.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with z.open(n) as src, open(path, "wb") as out:
                shutil.copyfileobj(src, out)


def library_name(plat):
    return LIBRARY[plat.split("-")[0]]


def install(state, lang, say=None, progress=None, log=None, run=subprocess.run, plat=None, cancelled=None, cfg=None,
            model=None):
    """Download, check and name in runtime/voice.json the model `model` of the hub language's rows (None — the
    recommended one); -> the record (local.installed). Refused (lang, model, platform, ffmpeg, space, unpack, check,
    cancelled — `cancelled()` said so before the record was written) or fetch.Failed (the network, a checksum). cfg —
    the hub's settings: without ffmpeg there (or in PATH) nothing is downloaded. The record keeps naming the model in use
    until the new one passes its check; the files no record names go after (prune)."""
    from core import catalog, fetch, i18n
    say = say or (lambda text: None)
    e, m = engine_pin(plat), model_pin(lang, model)
    if not m:
        if model and model_pin(lang):
            raise Refused(i18n.t("cli.setup.voice_no_model", model=model), "model")
        raise Refused(i18n.t("cli.setup.voice_no_lang"), "lang")
    if not e:
        raise Refused(i18n.t("cli.setup.voice_no_platform"), "platform")
    if cfg is not None and not has_ffmpeg(cfg):
        raise Refused(i18n.t("web.voicehelp.no_ffmpeg"), "ffmpeg")
    rt = local.runtime(state)
    downloads = os.path.join(state, "downloads")
    os.makedirs(downloads, exist_ok=True)
    os.makedirs(rt, exist_ok=True)
    ready = engine_ready(state, e["version"], e["platform"])   # in use by the model of now: not touched
    engine = [] if ready else [("lib", e["lib"], os.path.join(downloads, f"transcribe-native-{e['version']}.tar.gz")),
                               ("wrapper", e["wrapper"], os.path.join(downloads, f"transcribe_cpp-{e['version']}.whl"))]
    files = engine + [("vad", m["vad"], os.path.join(rt, "voice-models", m["vad"]["file"])),
                      ("model", m["model"], os.path.join(rt, "voice-models", m["model"]["file"]))]
    pending = [(k, pin, dest) for k, pin, dest in files
               if not (_present(dest, pin) and fetch.sha256(dest) == pin["sha256"])]
    total = sum(pin["size"] for _, pin, _ in pending)
    need = total + (3 * (e["lib"]["size"] + e["wrapper"]["size"]) if engine else 0)   # the archive and what it unpacks to
    if need and shutil.disk_usage(rt).free < need * 1.1:
        raise Refused(i18n.t("cli.setup.voice_no_space", gb=f"{need * 1.1 / 1e9:.1f}"), "space")
    done = 0
    say(i18n.t("cli.setup.voice_downloading", name=m["model"]["name"], mb=f"{total / 1e6:.0f}"))
    for _, pin, dest in pending:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fetch.download(pin["url"], dest, pin["sha256"], pin["size"], os.path.basename(dest),
                       i18n.t("cli.setup.voice_again"),
                       progress=(lambda d, t, off=done: progress and progress(off + d, total)), log=log, say=say)
        done += pin["size"]
    final = os.path.join(rt, "voice", e["version"])
    if engine:
        tmp = final + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            unpack_native(engine[0][2], os.path.join(tmp, "native"))
            unpack_wrapper(engine[1][2], os.path.join(tmp, "py"))
            if not os.path.isfile(os.path.join(tmp, "native", library_name(e["platform"]))):
                raise Refused(f"no {library_name(e['platform'])} in the archive", "unpack")
            shutil.rmtree(final, ignore_errors=True)
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        for _, _, path in engine:
            try:
                os.remove(path)
            except OSError:
                pass
    lib = os.path.join(final, "native", library_name(e["platform"]))
    vad_path, model_path = files[-2][2], files[-1][2]
    rec = {"engine": e["version"], "model": m["model"]["id"], "name": m["model"]["name"], "lang": m["model"]["lang"],
           "files": {"lib": {"path": os.path.relpath(lib, rt), "size": os.path.getsize(lib)},
                     "py": {"path": os.path.relpath(os.path.join(final, "py"), rt)},
                     "model": {"path": os.path.relpath(model_path, rt), "size": m["model"]["size"]},
                     "vad": {"path": os.path.relpath(vad_path, rt), "size": m["vad"]["size"]}}}
    if m["model"].get("language"):
        rec["language"] = m["model"]["language"]
    record = os.path.join(rt, local.RECORD)
    trial = record + ".check"
    catalog.write_private(trial, rec)
    say(i18n.t("cli.setup.voice_checking"))
    try:
        why, kind = _wont_load(state, trial, run)
        if why is not None:   # engine_failed: it does not load; else a recording held the lock, a time-out, a kill
            raise Refused(i18n.t("cli.setup.voice_no_load", output=why), "check" if kind == "engine_failed" else "check_later")
        if cancelled and cancelled():   # «Cancel» after the last block: the record stays as it was
            raise Refused("cancelled", "cancelled")
        os.replace(trial, record)
    finally:
        if os.path.exists(trial):
            os.remove(trial)
    drop_cloud(state)   # a local row chosen: the cloud row is no longer
    prune(state)
    return local.installed(state)


def prune(state, wait=PRUNE_WAIT):
    """The model files and engine versions voice.json does not name go — the earlier model after a switch, a cancelled
    or failed download's file — under the recognition lock: a recording that began on the earlier model ends on it.
    -> the names removed; a recording that holds the lock longer than `wait` — nothing now (the next install prunes).
    A file that will not go (gone already — «Turn off» at the same moment, no permission) is left: the record is what
    names the model in use, a leftover only takes room."""
    import platforms
    rt = local.runtime(state)
    if not os.path.isdir(rt):
        return []
    gone = []
    try:
        with platforms.current.exclusive(os.path.join(rt, local.LOCK), timeout=wait):
            rec = local.read_record(state)
            if rec is None:   # no record names anything: «Turn off» (remove) is what clears then, not this
                return []
            files = rec.get("files") if isinstance(rec.get("files"), dict) else {}
            keep = {os.path.basename(str((files.get(k) or {}).get("path") or "")) for k in ("model", "vad")}
            for folder, name in _entries(os.path.join(rt, "voice-models")):
                if name not in keep:
                    try:
                        os.remove(os.path.join(folder, name))
                        gone.append(name)
                    except OSError:
                        pass
            for folder, name in _entries(os.path.join(rt, "voice")):
                if name != str(rec.get("engine")):
                    shutil.rmtree(os.path.join(folder, name), ignore_errors=True)
                    gone.append(name)
    except (TimeoutError, OSError):
        return gone
    return gone


def _entries(folder):
    try:
        return [(folder, name) for name in sorted(os.listdir(folder))]
    except OSError:
        return []


def _wont_load(state, trial, run):
    """Why the engine and the model named by the record `trial` (a file in runtime/) did not pass the check: (text,
    kind) — kind engine_failed when they do not load or do not recognize, else what stopped the check (engine_busy,
    timeout, a process killed — out of memory) — or (None, None) when they did: voice.json is written only after this."""
    try:
        r = run([sys.executable, os.path.join(REPO, "modules", "voice", "local.py"), "--check", "--state", state,
                 "--record", os.path.basename(trial)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                timeout=CHECK_WAIT)
    except (OSError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}"[:400], "timeout" if isinstance(e, subprocess.TimeoutExpired) else "start"
    if r.returncode == 0:
        return None, None
    try:
        kind = json.loads((r.stdout or "").strip().splitlines()[-1]).get("error")
    except (ValueError, IndexError, AttributeError):
        kind = None
    return ((r.stderr or "") + (r.stdout or "")).strip()[-400:] or str(r.returncode), kind or f"exit {r.returncode}"


def _off_mark(state):
    return os.path.join(state, "setup", "voice.off")


def mark_off(state):
    """«Turn off» was pressed: a step that has not started yet (its .cancel would be dropped by its own start) or runs
    sees it and ends cancelled. «Turn on» clears it before the step starts."""
    os.makedirs(os.path.dirname(_off_mark(state)), exist_ok=True)
    with open(_off_mark(state), "w", encoding="utf-8"):
        pass


def clear_off(state):
    """«Turn on» or a press on a model: neither an earlier «Turn off» nor an earlier «Cancel» holds the new step."""
    for path in (_off_mark(state), _stop_mark(state)):
        try:
            os.remove(path)
        except OSError:
            pass


def _stop_mark(state):
    return os.path.join(state, "setup", "voice.stop")


def mark_stop(state):
    """«Cancel» of a switch: like mark_off, it holds a step that has not written its file yet (its own start drops a
    .cancel), but the model in use stays."""
    os.makedirs(os.path.dirname(_stop_mark(state)), exist_ok=True)
    with open(_stop_mark(state), "w", encoding="utf-8"):
        pass


def stop_marked(state):
    return os.path.exists(_stop_mark(state))


def off_marked(state):
    return os.path.exists(_off_mark(state))


def remove(state):
    """Voice off: the record first (from that moment nothing uses the files), then the files, the engine's downloads
    included. -> True when anything was there."""
    rt = local.runtime(state)
    had = False
    for path in (os.path.join(rt, local.RECORD), os.path.join(rt, local.RECORD + ".check"),
                 *(os.path.join(rt, name) for name in CLOUD_FILES)):
        if os.path.exists(path):
            os.remove(path)
            had = True
    for d in ("voice", "voice-models"):
        if os.path.isdir(os.path.join(rt, d)):
            shutil.rmtree(os.path.join(rt, d), ignore_errors=True)
            had = True
    downloads = os.path.join(state, "downloads")
    for name in os.listdir(downloads) if os.path.isdir(downloads) else ():
        if re.match(r"transcribe(-native|_cpp)-", name):
            os.remove(os.path.join(downloads, name))
            had = True
    return had


def status(state, lang, plat=None, cfg=None):
    """For the page and `hub setup`: {on, model, name, lang, engine, hears, models…} when installed — hears: the model
    knows the hub's language of now (the hub's language may have changed since) — else {on: false, offer…}. The cloud
    row chosen: model chatgpt and cloud — its words for the page (modules/voice/chatgpt.summary)."""
    rec = local.installed(state)
    out = offer(lang, plat, cfg, state)
    if cloud_on(state) and not out.get("why"):
        from modules.voice import chatgpt
        return {**out, "on": True, "model": CLOUD, "name": "ChatGPT", "lang": base(lang), "hears": True,
                "cloud": chatgpt.summary(state, rec.get("name") if rec else None)}
    raw = None if rec else local.read_record(state)
    missing = raw is not None   # a record whose files are gone: recordings are routed to it (transcribe.enabled)
    if missing:
        rec = {k: raw.get(k) for k in ("engine", "model", "name", "lang")}
    if not rec:
        return {"on": False, **out}
    known = ((_catalog().get("list") or {}).get(rec.get("model")) or {}).get("languages")
    hears = any(base(x) == base(lang) for x in known) if isinstance(known, list) else rec.get("lang") == base(lang)
    return {**out, "on": True, "model": rec.get("model"),
            "name": rec.get("name"), "lang": rec.get("lang"), "engine": rec.get("engine"), "hears": hears,
            **({"missing": True} if missing else {})}
