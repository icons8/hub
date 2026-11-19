"""Voice of the reply — a facade (module extraction spec; Module API 2.8, capability speech): who voices the reply to the owner in
Telegram. The bot asks it, not speak.py directly.

- builtin — speak.py in the core tree: text compression, Codex realtime, ffmpeg (behaviour unchanged);
- module — a process with the speech capability: the one named in hub.json `modules.use.speech` or the only ready one (hello, doctor)
  — which one leads is decided by hubd (core/providers.py, st["providers"]["speech"]). The hub calls its point speech.say
  {text, channel, session?} → {path, seconds, said} | {error} | {} (core/modrun.py POINTS). No answer (not ready,
  paused, timeout, failure) — there is no voice message for this reply, the owner already has the text: the builtin voice does not voice it
  (one provider per reply — there are never two voice messages). The bot records every module voicing as the fact speech.said:
  SPEECH_FAILS failures in a row — the following replies are voiced by the builtin one.

Voice is still switched on by `voice_reply.enabled` in hub.json — for a module too: without it the hub does not ask the module.
The file named by the module is not deleted by the hub: it is copied to its own temp folder (the bot removes it after sending) and only
if it is OGG (OggS) up to MAX_BYTES — otherwise any file whose path the module returned would go to Telegram.
"""
import os
import shutil
import stat
import tempfile

from core import cutout

# the built-in voice, or none: the open flavor is cut without it (core/cutout.py) — then only a module voices
if cutout.present("speech"):
    from modules.voice import speak
    from modules.voice.speak import SpeakError
else:
    speak = None

    class SpeakError(RuntimeError):
        """The voice module failed to voice the reply: the reason goes to Telegram as one line."""

BUILTIN = "builtin"
CAP = "speech"
POINT = "speech.say"
CHANNEL = "telegram"   # where the voice goes: to the module, to choose the voice and language for the channel
MAX_BYTES = 20 << 20   # a one-minute voice message is tens of KB; Telegram accepts up to 50 MB
OGG_MAGIC = b"OggS"


def active(cfg, recs):
    """The module that voices now (named or chosen by hubd), or None — the builtin voice."""
    from core import providers
    return providers.active(CAP, cfg, recs)


def enabled(cfg):
    """Reply voice is on (the "Voice reply" switch: voice_reply.enabled) — for the builtin voice and for a module."""
    return bool(((cfg or {}).get("voice_reply") or {}).get("enabled"))   # as speak.enabled: the same switch without it


def _take(name, path):
    """A module's voice message — copied into our own hub-speak-* folder (as with the builtin: <folder>/reply.ogg). Not OGG, not a regular
    file, empty or larger than MAX_BYTES — SpeakError."""
    from core import i18n   # lazily: the shown reason goes to the owner (the bot's speech.said, hub modules)
    why = i18n.t("voice.module_not_voice", name=name, mb=MAX_BYTES >> 20)
    try:
        # non-blocking open: a path to a channel (FIFO) would otherwise hang the voicing
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError):   # ValueError — a NUL byte in the path
        raise SpeakError(why) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SpeakError(why)
        with os.fdopen(fd, "rb", closefd=False) as src:
            data = src.read(MAX_BYTES + 1)
    except OSError:
        raise SpeakError(why) from None
    finally:
        os.close(fd)
    if not (0 < len(data) <= MAX_BYTES and data.startswith(OGG_MAGIC)):
        raise SpeakError(why)
    folder = tempfile.mkdtemp(prefix="hub-speak-")
    try:
        out = os.path.join(folder, "reply.ogg")
        with open(out, "wb") as f:
            f.write(data)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return out


class Speech:
    """One per bot. records() — state["modules"] of the process (the bot has its own journal reader); providers() — its
    state["providers"] (None — a copy of hubd's from disk); mods — module calls (core/modrun.Modules; None — create, only
    when a module voices). state_dir — the hub state folder. by — who voiced the last reply (a module or
    None — the builtin one): the bot records the outcome as the fact speech.said."""

    def __init__(self, state_dir, records=None, mods=None, providers=None):
        self.state_dir, self.records, self.mods, self.providers = state_dir, records, mods, providers
        self.by, self.silent = None, False   # silent — the module did not answer (as opposed to saying "no voice message": {})

    def recs(self):
        if self.providers is not None:
            return self.providers() or {}
        from core import providers
        return providers.current(self.state_dir)

    def _mods(self):
        if self.mods is None:
            from core import modrun
            self.mods = modrun.Modules(self.state_dir, records=self.records)
        return self.mods

    def reply(self, text, cfg, account=None, session=None):
        """(path to the ogg in a hub-speak-* temp folder, seconds, what was said) — the bot sends it and removes the folder; None —
        no voice message (the module is silent). On failure — SpeakError with a reason (a line in Telegram).
        account — the pool account for text compression by the builtin voice; session — whose reply it is (for the module)."""
        name, self.silent = active(cfg, self.recs()), False
        self.by = name
        if name is None:
            return speak.reply(text, cfg, account=account) if speak else None   # no built-in voice: the reply stays text
        mods = self._mods()
        if mods.ready(name) is None:
            return None   # not up yet (hub restart, after a crash) or paused — not a voicing failure
        got = mods.call(name, POINT, {"text": text, "channel": CHANNEL, **({"session": session} if session else {})})
        if got is None:
            self.silent = True
            return None
        if not got:
            return None
        if "error" in got:
            raise SpeakError(got["error"])
        return _take(name, got["path"]), max(1, round(got["seconds"])), got["said"]

