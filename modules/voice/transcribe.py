"""The "Voice" module: speech → text (recognition of voice messages by an external command — VOICE_CMD,
also as a module: once configured, the web gets a microphone and Telegram gets recognition). A library: it is called by the Telegram module
(the owner's voice messages) and by the hub page (the microphone in the input field).

Setup — `voice` in hub.json: `cmd` — the recognition command with `{wav}` (for example
`stt-tool --transcribe-file {wav} --json`), `ffmpeg` (from PATH or Homebrew by default),
`backoff` — pauses between attempts. The VOICE_CMD variable takes precedence over the settings. No command — the built-in
model if a person turned it on (modules/voice/local.py, install.py: downloaded into the state folder, run the same way as
a command); neither — no voice: neither microphone nor recognition. Any audio that ffmpeg understands (ogg from Telegram, webm from the browser) → 16 kHz mono WAV → the command →
the text from the last JSON line of its output ({"text": …}). If it failed (exit code not 0) but managed to transcribe a part, it may
print {"partial": …}: the error carries it (VoiceError.partial). The error also carries kind — a short reason code
(without paths and command output): the hub page uses it to tell the owner what broke.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import platforms

BACKOFF = (10, 30, 90, 270)  # 5 attempts
LOCAL_BACKOFF = (10, 30, 90)   # the built-in model: only "busy with another recording" is worth waiting for (no network)
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local.py")
# the cloud row (modules/voice/chatgpt.py, spec d2140189): the team flavor only — the open one has no such file
CLOUD = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bin", "hub-part")
CLOUD_CODES = ("cloud_login", "cloud_limit", "cloud_failed")
CLOUD_WAIT = 30
TIMEOUT = 300
# we name the ffmpeg input format instead of guessing: audio from outside could turn out to be a playlist or a network stream
FORMATS = {".ogg": "ogg", ".oga": "ogg", ".opus": "ogg", ".mp4": "mp4", ".m4a": "mp4", ".webm": "webm",
           ".wav": "wav", ".mp3": "mp3", ".flac": "flac"}
MIME_SUFFIX = {"audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
               "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/wav": ".wav",
               "audio/x-wav": ".wav", "audio/flac": ".flac", "audio/webm": ".webm", "video/mp4": ".mp4"}


class VoiceError(RuntimeError):
    partial = ""   # what the command managed to transcribe before the failure ({"partial": …} in its output) — show it to the owner
    kind = ""      # reason code: format, ffmpeg_start, ffmpeg_timeout, ffmpeg_failed, engine_start, engine_timeout, engine_failed,
    # engine_output; the built-in model also model_missing, engine_busy (local.CODES); the cloud row cloud_login,
    # cloud_limit, cloud_failed (CLOUD_CODES); "" — other


def _fail(text, kind):
    err = VoiceError(text)
    err.kind = kind
    return err


class Empty(VoiceError):
    """The provider heard no speech — the same VoiceError "empty transcription", as a separate class (Module API: {empty})."""


class Stopped(VoiceError):
    """The call was interrupted: cancel is set (cancelled) or the deadline (time.monotonic) has passed — the processes are stopped."""

    def __init__(self, why):
        super().__init__("cancelled" if why == "cancelled" else "deadline passed")
        self.why = why


def command(cfg):
    return (os.environ.get("VOICE_CMD") or ((cfg or {}).get("voice") or {}).get("cmd") or "").strip()


def _state(state):
    if state:
        return state
    from core import runtime
    return runtime.state_dir()


def local_argv(state=None):
    """The built-in model's command ({wav} in it), when a person turned it on here (there is a record); else None. The
    files the record names are checked by local.py under the recognition lock, not here: a switch of the model could
    remove them between this look and the recording (spec d2126783 §6.3) — model_missing comes from there."""
    from modules.voice import local
    state = _state(state)
    return [sys.executable, LOCAL, "{wav}", "--state", state] if state and local.read_record(state) else None


def cloud_argv(state=None):
    """The cloud row's command ({wav} in it) when a person chose it here and its code is in this tree; else None."""
    from core import cutout
    if not cutout.present("voice_cloud"):
        return None
    from modules.voice import chatgpt
    state = _state(state)
    if not (state and chatgpt.chosen(state)):
        return None
    from core import i18n
    from modules.voice import install
    lang = install.base(i18n.current())
    return [sys.executable, CLOUD, "voice_cloud", "{wav}", "--state", state, *(["--lang", lang] if lang else [])]


def enabled(cfg, state=None):
    """Voice is set up: a recognition command, the cloud row is chosen, or the built-in model is installed."""
    return bool(command(cfg)) or bool(local_argv(state)) or bool(cloud_argv(state))


def ffmpeg(cfg):
    return ((cfg or {}).get("voice") or {}).get("ffmpeg") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def suffix_for(media):
    """File suffix for a Telegram voice message, audio or video note — by what Telegram says about it; None — a type
    that transcription does not accept."""
    if "length" in media:   # a video note is always mp4
        return ".mp4"
    mime = str(media.get("mime_type") or "").split(";")[0].strip()
    return suffix_of_mime(mime) if mime else ".ogg"   # a voice message has no type: it is Opus in Ogg


def suffix_of_mime(mime):
    """Suffix by sound type ("audio/ogg; codecs=opus" → ".ogg"); None — a type that transcription does not accept."""
    return MIME_SUFFIX.get(str(mime or "").split(";")[0].strip().lower())


def sniff(raw):
    """Suffix by the first bytes of the recording (the containers that transcription accepts); None — not recognized. The browser could
    name the recording with the wrong type: Safari writes mp4 (AAC), and with an empty mimeType the page substituted audio/webm."""
    head = bytes(raw[:12])
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head[:4] == b"OggS":
        return ".ogg"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    return None


def _run(argv, timeout, cancel, deadline, **kw):
    """A command in its own process group: on timeout, on cancel (threading.Event) and on deadline
    (time.monotonic) it is stopped as a whole — ffmpeg, the recognition command and its descendants (also without cancel and deadline: the page,
    Telegram — with subprocess.run only the direct process was stopped on timeout, descendants stayed orphans). A process that has exited —
    its result stands, even if the time ran out while the output was being read."""
    if cancel is not None and cancel.is_set():
        raise Stopped("cancelled")
    end = time.monotonic() + timeout if deadline is None else min(deadline, time.monotonic() + timeout)
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **platforms.current.apart(),
                          **kw) as p:
        while True:
            try:
                left = end - time.monotonic()   # without cancel there is nothing to wait for except the deadline
                out, err = p.communicate(timeout=max(0.01, left if cancel is None else min(0.1, left)))
                return subprocess.CompletedProcess(argv, p.returncode, out, err)
            except subprocess.TimeoutExpired:
                if not (cancel is not None and cancel.is_set()) and time.monotonic() < end:
                    continue
            if p.returncode is None:   # we do not touch a reaped one: its group number could have gone to someone else
                try:
                    platforms.current.kill_detached(p, hard=True)
                except OSError:
                    p.kill()
            try:
                out, err = p.communicate(timeout=5)   # read the rest of the pipes: the group is stopped, they will close
                if p.returncode == 0:   # exited by itself between checks — its result
                    return subprocess.CompletedProcess(argv, 0, out, err)
            except subprocess.TimeoutExpired:
                pass   # a descendant outside the group holds the pipe — do not wait for it (Popen.__exit__ will close our ends)
            if cancel is not None and cancel.is_set():
                raise Stopped("cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise Stopped("timeout")
            raise subprocess.TimeoutExpired(argv, timeout)


def _partial(stdout):
    """Partial transcription of a failed command: the partial field of the last JSON line of its output, otherwise ""."""
    for line in reversed(stdout.splitlines()):
        if line.lstrip().startswith("{"):
            try:
                got = json.loads(line).get("partial")
            except (ValueError, AttributeError):
                return ""
            return " ".join(got.split()) if isinstance(got, str) else ""
    return ""


def transcribe(audio, cfg, suffix=".ogg", cancel=None, deadline=None, state=None):
    """Audio bytes → text; VoiceError — what is wrong (Empty — no speech; Stopped — cancel or deadline). The command
    first; without it the cloud row when chosen (its refusal — the local model on disk recognizes the recording, the
    refusal is noted for the row), then the local model."""
    cmd = command(cfg)
    if cmd:
        plan = [("cmd", platforms.current.split_command(cmd))]   # Windows: backslash paths stay intact
    else:
        plan = [(who, argv) for who, argv in (("cloud", cloud_argv(state)), ("local", local_argv(state))) if argv]
    if not plan:
        raise VoiceError("voice is not set up (voice.cmd in hub.json, or the built-in model)")
    fmt = FORMATS.get(suffix.lower())
    if not fmt:
        raise _fail(f"audio {suffix} is not supported", "format")
    with tempfile.TemporaryDirectory(prefix="hub-voice-") as tmp:
        src, wav = os.path.join(tmp, "in" + suffix), os.path.join(tmp, "voice.wav")
        with open(src, "wb") as f:
            f.write(audio)
        try:
            conv = _run([ffmpeg(cfg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                         "-protocol_whitelist", "file", "-f", fmt, "-i", src,
                         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav], 120, cancel, deadline)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise _fail(f"ffmpeg: {e}", "ffmpeg_timeout" if isinstance(e, subprocess.TimeoutExpired) else "ffmpeg_start") from None
        if conv.returncode or not os.path.exists(wav):
            raise _fail(f"ffmpeg rc={conv.returncode}: {conv.stderr.decode(errors='replace').strip()[-200:]}", "ffmpeg_failed")
        for i, (who, argv) in enumerate(plan):
            try:
                return _recognize(argv, wav, who, cancel, deadline)
            except (Empty, Stopped):   # no speech is the answer; a cancel or a deadline stops the fallback too
                raise
            except VoiceError as e:
                if who != "cloud":
                    raise
                rest = plan[i + 1:]
                _cloud_refused(state, e.kind, bool(rest))
                if not rest:
                    raise


def _recognize(argv, wav, who, cancel, deadline):
    """One recognizer on the WAV: the command (cmd), the cloud row or the local model -> the text."""
    # the cloud has its own time: 30 s and the length of the recording (16 kHz, 16 bit: 32000 bytes a second), then
    # the local model still has its own
    timeout = CLOUD_WAIT + os.path.getsize(wav) // 32000 if who == "cloud" else TIMEOUT
    try:
        out = _run([a.replace("{wav}", wav) for a in argv], timeout, cancel, deadline, stdin=subprocess.DEVNULL)
        # bytes — and parsing is done here: on Windows text=True decodes in the reader thread, and non-UTF-8 output gave
        # stdout None instead of an error (ValueError — the output is not UTF-8)
        stdout = out.stdout.decode("utf-8")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        raise _fail(f"recognition: {e}", "engine_output" if isinstance(e, ValueError) else
                    "engine_timeout" if isinstance(e, subprocess.TimeoutExpired) else "engine_start") from None
    if out.returncode and who != "cmd":   # the built-in recognizers name their failure: {"error": code}
        code = _local_error(stdout)
        if code:
            raise _fail(f"built-in {'cloud' if who == 'cloud' else 'model'}: {code}", code)
    if out.returncode:
        lines = [x for x in out.stderr.decode("utf-8", "replace").splitlines() if x.strip()]
        err = _fail(f"recognition rc={out.returncode}: {lines[-1] if lines else ''}".strip(), "engine_failed")
        err.partial = _partial(stdout)
        raise err
    try:
        text = json.loads(next(x for x in reversed(stdout.splitlines()) if x.lstrip().startswith("{")))["text"]
    except (StopIteration, ValueError, KeyError, TypeError):
        raise _fail("recognition returned no JSON with text", "engine_output") from None
    if not str(text).strip():
        raise Empty("empty transcription")
    return str(text).strip()


def _cloud_refused(state, why, fallback):
    """The cloud row refused: noted for the row (modules/voice/chatgpt.LAST) with the local model that takes over."""
    from modules.voice import chatgpt, local
    state = _state(state)
    by = (local.read_record(state) or {}).get("name") if fallback else None
    chatgpt.note_failure(state, why, by if isinstance(by, str) else None)
    print(f"voice: the cloud refused ({why}){' — the local model recognizes it' if fallback else ''}",
          file=sys.stderr, flush=True)


def _local_error(stdout):
    from modules.voice import local
    for line in reversed(stdout.splitlines()):
        if line.lstrip().startswith("{"):
            try:
                got = json.loads(line).get("error")
            except (ValueError, AttributeError):
                return None
            return got if got in local.CODES + CLOUD_CODES else None
    return None


def transcribe_retry(audio, cfg, suffix=".ogg", sleep=time.sleep, state=None):
    """With retries (network, the model has not woken up): pauses voice.backoff; the last error is raised. The built-in
    model: no network to wait for — only engine_busy (another recording holds it) is retried, LOCAL_BACKOFF."""
    local = not command(cfg)
    pauses = list(LOCAL_BACKOFF if local else ((cfg or {}).get("voice") or {}).get("backoff") or BACKOFF)
    for attempt in range(len(pauses) + 1):
        try:
            return transcribe(audio, cfg, suffix, state=state)
        except Empty:   # no speech — a retry will hear the same (before: 5 attempts, ~6.7 min of pauses for an empty voice message)
            raise
        except VoiceError as e:
            if attempt == len(pauses) or (local and e.kind != "engine_busy"):
                raise
            sleep(pauses[attempt])
