// Voice recording — the shared mechanism of the voice message (voice.js) and of dictation into a field (dictate.js,
// API 1.31): the microphone permission, "hold — speak — release" with the button and with Space in an empty field, a
// click starts recording and another click stops it, Esc cancels, transcription via /api/voice; a recording stops by
// itself after a long silence or at the time limit (autostop.js). What to do with the
// recording and where to show an error is decided by the target, the field being spoken into:
//   { key, field, kind: "message" | "dictate", empty(), deliver(rec, send), say(text), lock?(), begin?(), settle?(),
//     spaceStops?, spaceStarts? }
// key is the field key (draft, error), field is the field node, empty() means the field is empty (Space in it records),
// deliver({data, mime, blob, session: key}, send) — the recording is ready (send — what lock() gave when it stopped: the
// message addressed then, voice.js), lock() — address the voice message now (composer.js), say(text) — tell the owner ("" clears it), begin() —
// the press is accepted (dictation remembers the caret), settle() — the recording ended (without it: focus the field,
// caret at the end), spaceStops: false — while a click-started recording runs, Space in this field is just a space (the
// field stays visible); spaceStarts: false — Space in this field does not start recording (search: the button only).
// Dictation by Space works only while the caret is in its field: outside fields Space is again for the voice message
// (the latest message field).
import { $, OWN } from "./dom.js";
import { S } from "./state.js";
import { post } from "./api.js";
import { refreshBoxes } from "./boxes.js";
import { t } from "./i18n.js";
import { AUTOSTOP, levelDb, newWatch, step } from "./autostop.js";

// target is the target of the current (or latest) recording
// insert: "Done" was pressed — the words go into the field instead of being sent; took: how long the recording being recognized
// ran, ms (the clock stays on while it is recognized); job: the recognition in flight (a cancel makes it stale, its answer is dropped)
export const V = { rec: null, stream: null, since: 0, busy: false, opening: false, cancelled: false, session: null, stopAsap: false, target: null,
  insert: false, took: 0, job: 0 };

export const recording = () => !!V.rec || V.busy || V.opening || !!press;
export const pressed = () => press;
export const dictating = () => V.target?.kind === "dictate" && recording();
// Why recording is impossible: "" — it is possible; otherwise a key of the human-readable text. Safari (and any
// browser) gives the microphone only to a secure page — over http://address it is absent altogether (mediaDevices
// does not exist), and the button used to vanish silently
export const blocked = () => (!window.isSecureContext ? "web.voice.https_only"
  : !window.MediaRecorder || !navigator.mediaDevices?.getUserMedia ? "web.voice.unsupported" : "");
export const canRecord = () => !!S.snap?.voice && !blocked();
// the microphone icon: if voice is configured it is shown, also where recording is unavailable (pressing it says why)
export const voiceShown = () => !!S.snap?.voice;

// Hold — speak — release — sent, like in Telegram: hold Space for a long time, speak, release to send. The microphone
// button and Space in an empty field; a short click on the button works as before (recording, another click sends),
// a short Space press is just a space.
const HOLD_MS = 350;
const ASKED_MS = 1200;  // longer — the browser was asking for microphone permission
// how long we wait for the browser to open the microphone; longer — say so in words (getUserMedia did not answer at
// all: the page has permission, but macOS or Chrome do not hand the microphone over; pressing and Space silently did nothing)
const OPEN_MS = 5000;
const MIC_STUCK = t("web.voice.mic_stuck");
let press = null;       // {at, kind: "pointer" | "space", id?} — the button or Space is held down
let spaceTimer = null;  // Space pressed, not yet clear whether it is a space or voice
let current = null;     // the target: the field being spoken into
let message = null;     // the latest message field (a session's or hub.input): Space outside fields goes to it after dictation
const targets = new WeakMap();   // field -> target: the focused field becomes the current one (API 1.7)

export const currentTarget = () => current;
export function setCurrent(target) {
  current = target;
  if (target && target.kind !== "dictate") message = target;
}
// a field knows its target; when focused it is the current one (unless a recording is running)
export function bind(field, target) {
  targets.set(field, target);
  field.addEventListener("focus", () => { if (!V.rec && !press && targets.get(field) === target) setCurrent(target); });
}
// Space outside the dictation field: if dictation was current, go back to the message field (if it is still there) or
// the session field
const messageTarget = () => (message?.field?.isConnected ? message : targets.get($("msg")) || null);

// Whether the page may use the microphone — via the Permissions API, asked in advance rather than awaited in the press
// handler (waiting there is not allowed: the gesture would be lost). If permitted, a long open means a slow start (iOS
// takes a second or two to start the audio session), not a permission dialog, and we do not abandon the recording. If the
// browser cannot be asked or has not answered yet (permState null, "prompt" in Safari even after its own grant), we
// judge by whether it has handed out a stream on this page.
let permState = null, gaveStream = false;
const micOk = () => permState === "granted" || (gaveStream && permState !== "denied");
try {
  navigator.permissions.query({ name: "microphone" }).then((p) => {
    permState = p.state;
    p.onchange = () => { permState = p.state; };
  }, () => {});
} catch { /* no Permissions API */ }

// getUserMedia, but no longer than OPEN_MS: no answer — STUCK, and if the stream does arrive later, close it at once
const STUCK = new Error("the microphone did not answer");
function openMic() {
  const p = navigator.mediaDevices.getUserMedia({ audio: true });
  return new Promise((ok, fail) => {
    const t = setTimeout(() => { fail(STUCK); p.then((st) => st.getTracks().forEach((x) => x.stop()), () => {}); }, OPEN_MS);
    p.then((st) => { clearTimeout(t); gaveStream = true; ok(st); }, (e) => { clearTimeout(t); fail(e); });
  });
}
// what to say about a getUserMedia refusal: "allow this page" only when the page itself is refused. The page allowed (or
// Chrome's "Permission denied by system") — the system does not give the browser the microphone; NotReadableError — it
// is busy or the system refused it; NotFoundError — there is no input at all
function refusal(e) {
  const name = e?.name || "";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "web.voice.mic_none";
  if (name !== "NotAllowedError" && name !== "SecurityError") return "web.voice.mic_busy";
  return permState === "granted" || /system/i.test(e?.message || "") ? "web.voice.mic_system" : "web.voice.mic_denied";
}

// getUserMedia is called inside the press handler itself, with no await before it: iOS Safari hands out the microphone
// only from a gesture, and the permissions.query answer arrives after the gesture (it may not exist at all). Whether
// permission was needed shows in the time it took to open (ASKED_MS in start): if the browser asked, there will be no
// recording, press again.
export function pressStart(kind, at = Date.now()) {
  if (V.rec || V.busy || V.opening || press || !current) return;
  const target = current;
  V.target = target;   // before the refusal too: its redraw settles this target, not the one of an earlier dictation
  target.begin?.();
  const why = blocked();
  if (why) { target.say(t(why)); return redraw(); }
  press = { at, kind };
  V.session = target.key;
  start(target);
}
// a click-style recording started from code (hub.input().record(), API 1.32 feature inputRecord): the same start as a
// short press of the microphone button (the second click on the button or Space sends it), taken from the caller's own
// click, because getUserMedia is called synchronously inside pressStart. false — it is impossible (no voice, no secure page
// or MediaRecorder) or another recording is going: nothing is said to the owner, the caller knows what to do
export function startFor(target) {
  if (!canRecord() || recording()) return false;
  setCurrent(target);
  pressStart("pointer");
  pressEnd();   // a click, not a hold: the recording goes on until the next click
  return recording();
}
// the pointer that holds the button: releasing a different finger does not end the recording
export function holdPointer(id) { if (press?.kind === "pointer") press.id = id; }

export function pressEnd() {
  if (!press) return;
  const held = Date.now() - press.at;
  press = null;
  if (held >= HOLD_MS) { if (V.rec) V.rec.stop(); else V.stopAsap = true; }  // held and released — send
  else redraw();  // a short click — recording goes on, the second click sends
}

// the release is caught on the document — the button will be rebuilt; on a long press iOS sends pointercancel instead of
// pointerup (selection menu, system gesture) — without it the hold would stay forever
for (const type of ["pointerup", "pointercancel"]) document.addEventListener(type, (e) => { if (press?.kind === "pointer" && e.pointerId === press.id) pressEnd(); });

// stop the recording — and use it (send, insert)
export function stop() { press = null; V.rec?.stop(); }
// "Done": stop the recording, the recognized words go into the field and are not sent
export function stopInto() { if (!V.rec) return; press = null; V.insert = true; V.rec.stop(); }
// cancel: the recording, the microphone opening or the recognition — with no effect
export function cancel() {
  press = null;
  V.stopAsap = false;
  if (V.busy && !V.rec) {   // recognizing: the answer, when it comes, is dropped; the field is free at once
    V.job++;
    V.busy = false;
    return redraw();
  }
  V.cancelled = true;
  if (V.rec) V.rec.stop();
  redraw();
}

export const curField = () => current?.field?.isConnected ? current.field : $("msg");
const fieldEmpty = () => !!current && canRecord() && current.empty();
const textField = (n) => /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(n?.tagName || "") || !!n?.isContentEditable;

document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
  const focused = targets.get(document.activeElement);
  if (focused?.spaceStarts === false && !V.rec) return;   // a button-only field (search): Space is a space
  if (!V.rec && !press) {
    if (focused) setCurrent(focused);   // speak into the field where the cursor is: a session's, a plugin's, a dictation one
    else if (current?.kind === "dictate") current = messageTarget();   // the cursor left the dictation field — voice message
  }
  const inBox = document.activeElement === curField(), elsewhere = textField(document.activeElement);
  if (V.rec && !press && (inBox || !elsewhere)) {   // recording started by a click — Space sends
    if (inBox && V.target?.spaceStops === false) return;   // the field is visible (dictation): Space in it is a space
    e.preventDefault();
    V.rec.stop();
    return;
  }
  if ((!inBox && elsewhere) || !fieldEmpty()) return;
  e.preventDefault();
  spaceTimer = setTimeout(() => { spaceTimer = null; pressStart("space", Date.now() - HOLD_MS); }, HOLD_MS);
});

// the window lost focus mid-hold (permission dialog, another app) — the release will not come: treat it as released
window.addEventListener("blur", () => { if (spaceTimer) { clearTimeout(spaceTimer); spaceTimer = null; } if (press) pressEnd(); });

document.addEventListener("keyup", (e) => {
  if (e.code !== "Space") return;
  if (spaceTimer) {  // released quickly — it was just a space
    clearTimeout(spaceTimer);
    spaceTimer = null;
    const f = curField();
    if (f && document.activeElement === f) typeText(f, " ");
    return;
  }
  if (press?.kind === "space") pressEnd();
});

// text into the field at the caret (replacing the selection) and an input event, as from the keyboard: execCommand —
// undoable with ⌘Z; if it fails — by hand. execCommand writes into whatever has the focus: only when it is the field
// (a hidden field does not take the focus, and the text would go into another one)
export function typeText(field, text) {
  if (document.activeElement === field && document.execCommand("insertText", false, text)) return;
  if (field.isContentEditable) {
    const sel = getSelection(), r = sel.rangeCount && field.contains(sel.getRangeAt(0).startContainer) ? sel.getRangeAt(0) : null;
    const node = document.createTextNode(text);
    if (r) { r.deleteContents(); r.insertNode(node); } else field.append(node);
    const after = document.createRange();
    after.setStartAfter(node);
    sel.removeAllRanges();
    sel.addRange(after);
  } else if (field.selectionStart == null) field.value += text;   // email and others without a caret — at the end
  else field.setRangeText(text, field.selectionStart, field.selectionEnd, "end");
  field.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
}

// the recording clock and whatever paints its own view (dictation buttons): on a recording change and on each tick
const fmt = (ms) => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const clock = () => fmt(Date.now() - V.since);
export { clock, fmt };
const painters = new Set();
export const onChange = (fn) => painters.add(fn);
const paint = () => painters.forEach((fn) => fn());
setInterval(() => { for (const n of document.querySelectorAll(`[data-rec-clock]${OWN}`)) n.textContent = clock(); paint(); }, 500);
document.addEventListener("keydown", (e) => {
  // while recognizing — only when the strip with its cross is on screen (this field's): Esc is not taken for a recognition elsewhere
  if (e.key === "Escape" && (V.rec || V.opening || (V.busy && !dictating() && document.querySelector(".recstrip .rec-cancel")))) { e.stopPropagation(); cancel(); }
}, true);

async function start(target) {
  const asked = Date.now(), known = micOk();
  V.opening = true;
  V.cancelled = false;
  redraw();
  try {
    V.stream = await openMic();
  } catch (e) {
    V.opening = false;
    target.say(e === STUCK ? MIC_STUCK : t(refusal(e)));
    press = null;
    V.stopAsap = false;
    return redraw();
  }
  V.opening = false;
  if (V.cancelled) {   // "Cancel" or Esc while the microphone was opening
    V.stream.getTracks().forEach((t) => t.stop());
    V.stopAsap = false;
    return redraw();
  }
  // the browser showed the "allow microphone" dialog (Space was released while the dialog was open and the release got
  // lost, so the recording ran and could not be stopped): do not record blind — say so and wait for a new press
  if (!known && Date.now() - asked > ASKED_MS) {
    V.stream.getTracks().forEach((t) => t.stop());
    press = null;
    V.stopAsap = false;
    target.say(t("web.voice.mic_allowed_hold"));
    return redraw();
  }
  target.say("");
  const chunks = [];
  V.session = target.key;
  V.cancelled = false;
  V.since = Date.now();
  V.insert = false;
  V.rec = new MediaRecorder(V.stream);
  V.rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  V.rec.onstop = async () => {
    watchStop();
    V.stream.getTracks().forEach((t) => t.stop());
    const mime = V.rec.mimeType || chunks[0]?.type || "audio/webm";
    V.rec = null;
    const insert = V.insert;
    V.insert = false;
    if (V.cancelled || !chunks.length) return redraw();
    // "send" is pressed (or the silence stopped it): the voice message is addressed now — the field it was recorded in, its
    // reply, cards and files of this moment (composer.js lock), also when the stop was pressed on another session's page
    const send = insert ? null : target.lock?.() || null;
    const took = V.took = Date.now() - V.since;   // before the strip of "recognizing" is drawn: it is not redrawn for the same mode
    const mine = ++V.job;   // "Cancel" while the recording is being read (before it is sent for recognition) makes this stale too
    V.busy = true;
    redraw();
    const blob = new Blob(chunks, { type: mime });
    const data = await new Promise((ok) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",")[1] || ""); r.readAsDataURL(blob); });
    if (mine !== V.job) return send?.drop();
    target.deliver({ data, mime, blob, session: target.key, insert, took }, send);
  };
  V.rec.start();
  watchStart(V.stream);
  if (V.stopAsap) { V.stopAsap = false; V.rec.stop(); return; }  // released while the browser was turning the microphone on
  redraw();
}

// The recording stops by itself (autostop.js): the level is read from an AnalyserNode on the same stream, not guessed.
// If the audio context cannot be made or does not run (iOS may keep it suspended), only the time limit works.
// It plays nothing, so it has no output device (sinkId none, Chrome 110+; other browsers ignore the option): it opened
// a stream on the default output at every dictation (05.11: the owner's USB DAC). A browser that knows the option and
// refuses "none" gets the context without it, as before: else the level is lost and only the time limit stops a recording.
let watching = null;   // {timer, ctx} of the running recording
// a file picker opened from the recording field ("+"): the person is choosing a screenshot, so its quiet is not silence —
// the quiet is counted again once the picker closes (the time limit still works)
let picking = false;
export function picker(open) { picking = open; }
function watchStart(stream) {
  watchStop();
  let ctx = null, an = null, buf = null;
  try {
    try { ctx = new (window.AudioContext || window.webkitAudioContext)({ sinkId: { type: "none" } }); }
    catch { ctx = new (window.AudioContext || window.webkitAudioContext)(); }   // sinkId refused: the default output
    ctx.resume?.().catch?.(() => {});
    an = ctx.createAnalyser();
    an.fftSize = 8192;   // ~170 ms of sound per reading at 48 kHz: a word between two readings is not missed
    ctx.createMediaStreamSource(stream).connect(an);
    buf = new Float32Array(an.fftSize);
  } catch { ctx?.close?.().catch?.(() => {}); ctx = an = null; }
  const w = newWatch(Date.now(), !!an);
  const timer = setInterval(() => {
    let db = null;
    try {   // an older Safari (webkitAudioContext) has no getFloatTimeDomainData: no level, the time limit still works
      if (an?.getFloatTimeDomainData && ctx.state === "running") { an.getFloatTimeDomainData(buf); db = levelDb(buf); }
    } catch { db = null; }
    if (picking) db = null;   // no reading: nothing is judged by it
    const why = step(w, db, Date.now());
    // the button or Space is held: the release decides ("release to send"), only the time limit stops it
    if (!why || !V.rec || (press && why !== "max")) return;
    // once: the recorder's onstop comes later, the next ticks would stop it again. "nosound" is sent too: the level
    // is the page's guess (a quiet voice, a far microphone), "no speech" is decided by /api/voice and shown as usual
    watchStop();
    stop();
  }, AUTOSTOP.TICK_MS);
  watching = { timer, ctx };
}
function watchStop() {
  picking = false;
  if (!watching) return;
  clearInterval(watching.timer);
  watching.ctx?.close?.().catch?.(() => {});
  watching = null;
}

// recording → text: {data: {text}} or {error, status}; while waiting — "Recognizing"
// {discarded: true} — "Cancel" was pressed meanwhile: the answer is not wanted
export async function recognize(rec) {
  V.session = rec.session;   // "Retry" of an earlier recording: the "Recognizing" strip is that field's, not the last recording's
  const job = ++V.job;
  V.took = rec.took || 0;   // the strip shows this recording's length, also when it is an earlier one sent again
  V.busy = true;
  redraw();
  const res = await post("/api/voice", { data: rec.data, mime: rec.mime, session: rec.session });  // the addressee goes into the failure response
  if (job !== V.job) return { discarded: true };
  V.busy = false;
  return res;
}
// what to tell the owner about a failed transcription: 502 carries the cause in the hub's words (hubs before 1.30 give none)
export const failureText = (res) => (res.status === 502 && typeof res.data?.reason === "string" && res.data.reason ? `${res.error}: ${res.data.reason}` : res.error);
// 422 — no speech in the recording (server: an outcome, not a failure): a retry would hear the same
export const noSpeech = (res) => res.status === 422;

// redraw the page (a recording started/ended): the dock key changes with recording(); when it ended — focus the field
export function redraw() {
  import("./state.js").then(({ render }) => {
    render();
    refreshBoxes();
    paint();
    if (recording()) return;
    const target = V.target || current;
    if (target?.settle) return target.settle();
    const ta = curField();
    if (ta && !ta.isContentEditable) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); ta.dispatchEvent(new Event("input")); }
  });
}
