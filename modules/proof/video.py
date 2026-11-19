"""hub video: a video of a scenario in the browser, up to 60 s (an optional module).
Turned on by hand: "video": {"enabled": true} in hub.json; ffmpeg is needed (path: video.ffmpeg, otherwise from PATH).

Its own headless Chrome walks through the scenario steps, the CDP screencast gives frames with timestamps, ffmpeg joins them by those
timestamps into an H.264 MP4 (faststart: plays right in Telegram). The cursor is a yellow dot, a click is a red wave, a step has
a caption at the bottom. The empty start is trimmed: the recording runs from the end of the first navigation.

Scenario (JSON):
  {"mobile": false, "max_seconds": 60, "steps": [
     {"goto": "https://…"}, {"click": "<selector>", "caption": "Step 1 — save"},
     {"type": ["<selector>", "text"]}, {"press": "Enter"}, {"scroll": "<selector>" | 600},
     {"hover": "<selector>"}, {"wait": 800}, {"wait_for": "<selector>"}, {"caption": "Done"}]}
The steps are the same code as in `hub shot --steps` (modules/proof/steps.py).
"""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time

from core import i18n, perms
from modules.proof import cdp, steps
from modules.proof.steps import Cursor, Usage, run_step

MAX_SECONDS = 60


def enabled(cfg):
    return bool((cfg.get("video") or {}).get("enabled"))


def ffmpeg(cfg):
    path = os.path.expanduser((cfg.get("video") or {}).get("ffmpeg") or "") or shutil.which("ffmpeg")
    return path if path and os.path.exists(path) else None


# In order of preference: the encoder and its quality options. An LGPL ffmpeg build has no libx264; the hardware ones
# (nvenc, qsv, mf) are not here until checked on a real machine.
ENCODERS = (
    ("libx264", ["-preset", "veryfast", "-crf", "23"]),
    ("h264_videotoolbox", ["-b:v", "6M"]),
    ("libopenh264", ["-b:v", "6M"]),
    ("mpeg4", ["-q:v", "4"]),
)
_encoders = {}


def encoder(ff):
    """The ffmpeg options of the best available video encoder, found once per ffmpeg file."""
    try:
        key = (ff, os.stat(ff).st_mtime_ns)
    except OSError:
        key = (ff, 0)
    if key not in _encoders:
        try:
            p = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
            listed = {w[1] for w in (ln.split() for ln in p.stdout.splitlines()) if len(w) > 1 and w[0].startswith("V")}
        except (OSError, subprocess.SubprocessError):
            listed = set()
        _encoders[key] = next((["-c:v", n] + opts for n, opts in ENCODERS if n in listed), None)
    if not _encoders[key]:
        raise Usage(i18n.t("cli.proof.no_encoder"))
    return _encoders[key]


def load(path):
    """The scenario from a file; errors come at once and are clear, before the browser starts."""
    return steps.load(path)


def concat_list(frames, end):
    """A list for ffmpeg concat: every frame is held until the next one, the last until the end of the recording."""
    lines = ["ffconcat version 1.0"]
    for i, (path, t) in enumerate(frames):
        nxt = frames[i + 1][1] if i + 1 < len(frames) else end
        lines += [f"file '{path}'", f"duration {max(nxt - t, 0.001):.3f}"]
    if frames:
        lines.append(f"file '{frames[-1][0]}'")   # otherwise ffmpeg loses the duration of the last one
    return "\n".join(lines) + "\n"


class Recorder:
    def __init__(self, page, folder):
        self.page, self.folder, self.frames, self.caption = page, folder, [], None
        page.on_event = self.event

    def event(self, method, params):
        if method != "Page.screencastFrame":
            return
        path = os.path.join(self.folder, f"f{len(self.frames):05d}.jpg")
        with open(path, "wb") as f:
            f.write(base64.b64decode(params["data"]))
        self.frames.append((path, time.time()))   # arrival time: the same clock as the end of the recording
        self.page.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

    def start(self, w, h):
        self.page.call("Page.startScreencast", {"format": "jpeg", "quality": 85, "maxWidth": w, "maxHeight": h})
        self.t0 = time.time()

    def stop(self):
        self.page.call("Page.stopScreencast")
        return time.time()


def record(scenario, out, ff, chrome=None, timeout=60):
    """The scenario's MP4; returns (path, seconds). If a step fails, an error with the step number, no video."""
    encode = encoder(ff)   # no usable encoder: say so before the browser starts
    mobile = bool(scenario.get("mobile"))
    w, h = (390, 844) if mobile else (1280, 800)
    limit = min(float(scenario.get("max_seconds") or MAX_SECONDS), MAX_SECONDS)
    page = cdp.Page(chrome, timeout)
    folder = tempfile.mkdtemp(prefix="hub-video-")
    try:
        page.viewport(w, h, 2 if mobile else 1, mobile)
        page.call("Page.addScriptToEvaluateOnNewDocument", {"source": steps.OVERLAY})
        rec, cur = Recorder(page, folder), Cursor(page)
        todo = scenario["steps"]
        page.goto(cdp.url_of(todo[0]["goto"]), 0.3)
        page.js(steps.OVERLAY)
        rec.start(w * (2 if mobile else 1), h * (2 if mobile else 1))
        first = dict(todo[0])
        first.pop("goto")
        for i, s in enumerate([first] + todo[1:], 1):
            if time.time() - rec.t0 > limit:
                break
            try:
                run_step(page, cur, rec, s)
            except cdp.Error as e:
                raise cdp.Error(i18n.t("cli.proof.step_failed", n=i, error=e))
        page.pump(1)
        end = rec.stop()
        if not rec.frames:
            raise cdp.Error(i18n.t("cli.proof.no_frames"))
        start = rec.frames[0][1]
        frames = [(p, t - start) for p, t in rec.frames]
        listing = os.path.join(folder, "list.txt")
        with open(listing, "w") as f:
            f.write(concat_list(frames, end - start))
        p = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing,
                            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-r", "25", *encode,
                            "-movflags", "+faststart", "-t", str(limit), out],
                           capture_output=True, text=True, timeout=300)
        if p.returncode or not os.path.exists(out):
            raise cdp.Error(f"ffmpeg: {(p.stderr or '').strip()[-300:]}")
        return out, min(end - start, limit)
    finally:
        page.close()
        shutil.rmtree(folder, ignore_errors=True)


def main(argv, cfg, folder):
    try:
        if not enabled(cfg):
            raise Usage(i18n.t("cli.proof.video_off"))
        ff = ffmpeg(cfg)
        if not ff:
            raise Usage(i18n.t("cli.proof.no_ffmpeg"))
        args = list(argv)
        out = None
        if "--out" in args[:-1]:
            i = args.index("--out")
            out, args = args[i + 1], args[:i] + args[i + 2:]
        if len(args) != 1:
            raise Usage(i18n.t("cli.proof.video_usage"))
        # --out is the user's place: a new file there gets the user's permissions, not the hub's closed ones; an existing one keeps its own
        made = [os.path.abspath(out)] if out and not os.path.exists(out) else []
        if not out:   # as in hub shot: not in the session's repo
            os.makedirs(folder, exist_ok=True)
            out = os.path.join(folder, time.strftime("video-%Y%m%d-%H%M%S.mp4"))
        try:
            path, secs = record(load(args[0]), os.path.abspath(out), ff)
        finally:
            perms.give_to_user(made)
        print(i18n.t("cli.proof.video_done", path=path, seconds=f"{secs:.0f}"))
        return 0
    except (Usage, cdp.Error, OSError, subprocess.SubprocessError) as e:
        print(f"hub video: {e}", file=sys.stderr)
        return 2
