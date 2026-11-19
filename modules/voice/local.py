#!/usr/bin/env python3
"""The built-in voice: speech → text on this machine with a model the hub downloaded itself (spec d2109189: voice in
the base install). One process per recording, like the recognition command (transcribe.py runs it the same way):

    local.py <wav 16 kHz mono> [--state <state folder>]     → {"text": …}  or  {"error": code} with exit code 3
    local.py --check [--state <state folder>] [--record <file>]  → {"ok": true} when the engine and the model load
                                                                   and recognize 1 s of silence in the record's language

The engine is transcribe.cpp (MIT, the one inside Handy): its native library and its ctypes wrapper of the same
version, and the model with the speech detector (Silero), all pinned by sha256 and put into the state folder by the
setup step `voice` (modules/voice/install.py). `<state>/runtime/voice.json` names them; no record, a file gone or of
another size — model_missing. The speech detector marks speech; pieces closer than GAP seconds are joined while a
piece stays under MAX - 2*EDGE, a longer stretch is cut into equal parts, EDGE seconds are added at both ends; the model
is loaded only when there is speech (silence and noise give an empty text without it). A model that does not tell
languages itself gets the record's `language` (spec d2126783 §6.4). One recognition at a time per hub:
`<state>/runtime/voice.lock`, held by the OS on an open file (a process that dies frees it at once); waiting for it
longer than LOCK_WAIT — engine_busy. The record is read under the lock: a switch of the model (install.prune) removes
the earlier model's files under the same lock, so a recording runs on one model whole.

The stdlib only, no imports of the hub besides platforms (it runs as a script)."""
import array
import json
import os
import sys
import wave

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECORD = "voice.json"
LOCK = "voice.lock"
GAP, MAX, EDGE, RATE = 0.45, 25.0, 0.2, 16000
LOCK_WAIT = 120
FAILED = 3   # the exit code with {"error": code}
CODES = ("model_missing", "engine_busy", "engine_failed")


def runtime(state):
    return os.path.join(state, "runtime")


def read_record(state, name=RECORD):
    """voice.json (or the setup step's trial record `name`) as it is — a dict — or None."""
    try:
        with open(os.path.join(runtime(state), os.path.basename(name)), encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def installed(state, record=RECORD):
    """The installed engine and model: {engine, model, lang, lib, py, model_file, vad_file} with absolute paths, or None
    (no record, a path that leads out of runtime/, a file gone or of another size than the record says). record — the
    record's file in runtime/ (the setup step checks a trial one before it becomes voice.json)."""
    base = runtime(state)
    rec = read_record(state, record)
    if rec is None or not isinstance(rec.get("files"), dict):
        return None
    real = os.path.realpath(base)
    out = {k: rec.get(k) for k in ("engine", "model", "name", "lang")}
    if isinstance(rec.get("language"), str) and rec["language"]:
        out["language"] = rec["language"]
    for key in ("lib", "py", "model", "vad"):
        item = rec["files"].get(key)
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return None
        path = os.path.realpath(os.path.join(base, item["path"]))
        try:
            if os.path.commonpath([real, path]) != real:
                return None
        except ValueError:   # Windows: another drive
            return None
        size = item.get("size")
        if key == "py":
            if not os.path.isfile(os.path.join(path, "transcribe_cpp", "__init__.py")):
                return None
        elif not os.path.isfile(path) or (type(size) is int and os.path.getsize(path) != size):
            return None
        out["model_file" if key == "model" else "vad_file" if key == "vad" else key] = path
    return out


def quietest(x, at, rate=RATE, reach=0.5, frame=0.02):
    """The start of the quietest 20 ms within reach seconds of sample at: a seam there does not cut a word in half."""
    n = int(frame * rate)
    best, where = None, at
    for s in range(max(0, at - int(reach * rate)), min(len(x) - n, at + int(reach * rate)), n):
        loud = sum(abs(v) for v in x[s:s + n])
        if best is None or loud < best:
            best, where = loud, s
    return where


def pieces(spans, total, gap=GAP, cap=MAX, edge=EDGE, rate=RATE, x=None):
    """Speech spans [(start, end)] in samples → pieces to recognize: spans closer than gap joined while the piece stays
    within cap - 2*edge, a longer one cut into equal parts that meet without overlap (with the samples x — each seam
    moved to the quietest moment near it), edge seconds added at both ends of a stretch (within 0..total)."""
    inner = int((cap - 2 * edge) * rate)
    joined = []
    for a, b in spans:
        if b <= a:
            continue
        if joined and a - joined[-1][1] < gap * rate and b - joined[-1][0] <= inner:
            joined[-1] = (joined[-1][0], b)
        else:
            joined.append((a, b))
    out, pad = [], int(edge * rate)
    for a, b in joined:
        n = -(-(b - a) // inner)
        step = -(-(b - a) // n)
        seams = [a] + [quietest(x, s, rate) if x is not None else s for s in range(a + step, b, step)] + [b]
        for s, e in zip(seams, seams[1:]):   # the parts of one stretch meet without overlap: a word at a seam comes once
            out.append((max(0, s - pad) if s == a else s, min(total, e + pad) if e == b else e))
    return out


def samples(path):
    """A 16 kHz mono 16-bit WAV → float32 samples in -1..1."""
    with wave.open(path) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != RATE:
            raise ValueError("not a 16 kHz mono 16-bit WAV")
        raw = array.array("h")
        raw.frombytes(w.readframes(w.getnframes()))
    if sys.byteorder != "little":
        raw.byteswap()
    return array.array("f", (s / 32768.0 for s in raw))


def threads():
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def engine(rec):
    """The wrapper of the installed version, loading the installed library (TRANSCRIBE_LIBRARY: no search elsewhere)."""
    os.environ["TRANSCRIBE_LIBRARY"] = rec["lib"]
    sys.path.insert(0, rec["py"])
    import transcribe_cpp
    return transcribe_cpp


def recognize(rec, wav):
    tc = engine(rec)
    x = samples(wav)
    spans = [(s.start_sample, s.end_sample) for s in tc.Model(rec["vad_file"], backend="cpu").vad_session().run(x).segments]
    cut = pieces(spans, len(x), x=x)
    if not cut:
        return ""
    session = tc.Model(rec["model_file"], backend="cpu").session(n_threads=threads())
    lang = {"language": rec["language"]} if rec.get("language") else {}
    return " ".join(t for t in (session.run(x[a:b], **lang).text.strip() for a, b in cut) if t)


def check(rec):
    """The engine and the model load, the detector runs and the model recognizes 1 s of silence in the record's
    language (a language the model does not know fails here, before the record is switched to it; the text is not
    looked at — models make words up on silence, the detector keeps silence from them in recognize)."""
    tc = engine(rec)
    silence = array.array("f", [0.0] * RATE)
    tc.Model(rec["vad_file"], backend="cpu").vad_session().run(silence)
    lang = {"language": rec["language"]} if rec.get("language") else {}
    tc.Model(rec["model_file"], backend="cpu").session(n_threads=1).run(silence, **lang)


def _out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False), flush=True)
    return code


def main(argv):
    args = list(argv)
    opts = {}
    for flag in ("--state", "--record"):
        if flag in args:
            i = args.index(flag)
            opts[flag] = args[i + 1] if i + 1 < len(args) else None
            del args[i:i + 2]
    state = opts.get("--state")
    if not state:
        sys.path.insert(0, REPO)
        from core import runtime as rt
        state = rt.state_dir()
    if len(args) != 1:
        print("usage: local.py <wav> [--state <dir>] | --check", file=sys.stderr)
        return 2
    if not state or not os.path.isdir(runtime(state)):
        return _out({"error": "model_missing"}, FAILED)
    sys.path.insert(0, REPO)
    import platforms
    try:
        with platforms.current.exclusive(os.path.join(runtime(state), LOCK), timeout=LOCK_WAIT):
            rec = installed(state, opts.get("--record") or RECORD)
            if rec is None:
                return _out({"error": "model_missing"}, FAILED)
            try:
                if args == ["--check"]:
                    check(rec)
                    return _out({"ok": True})
                text = recognize(rec, args[0])
            except Exception as e:  # noqa: BLE001 — a model that does not load, the wrapper's errors, a bad WAV
                print(f"voice{' check' if args == ['--check'] else ''}: {type(e).__name__}: {e}"[:400], file=sys.stderr)
                return _out({"error": "engine_failed"}, FAILED)
    except TimeoutError:
        return _out({"error": "engine_busy"}, FAILED)
    return _out({"text": text})


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
