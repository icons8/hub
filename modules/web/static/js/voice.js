// Voice in the input field, as in Telegram (click — recording starts, click again — it is sent; no need to press send
// afterwards): one round icon on the right — a microphone while the field is empty, "send" when there is text or a
// file. With text or a file there is a second, quiet microphone next to "send": it dictates into the field at the
// caret (dictate.js) — the typed text stays, nothing is sent by itself. Recording: instead of the field there is a red dot, the time and × (Esc); the second click recognizes
// the speech (on the Mac) and sends it right away as a "🎤 <the word voice in the owner's language>: …" message, like a Telegram voice message. There is no microphone
// if voice is not configured or the browser does not provide it (only localhost or https): then the microphone icon
// stays and pressing it explains this in words. The recording itself (microphone, hold/click, Space, Esc) is recorder.js,
// shared with dictation into a field (dictate.js); here is the voice message: the field icon, the recording strip, sending.
import { $, el, icon } from "./dom.js";
import { S } from "./state.js";
import { t } from "./i18n.js";
import * as R from "./recorder.js";
import { dictateButton } from "./dictate.js";
import { fill, hold, release, settled } from "./outgoing.js";

// the modules import each other in a circle (api.js → voice.js → recorder.js → api.js): R.* only at call time, not at load
export const recording = () => R.recording();
// a voice message is being recorded (not dictation into a plugin field)
const live = () => !R.dictating();
// for the input field's key: when the mode changes, rebuild the field (held → released briefly: different hint and button)
export const voiceMode = () => {
  if (!live()) return "";
  const press = R.pressed();
  return R.V.busy ? "busy" : R.V.opening ? `opening-${press ? press.kind : ""}` : press ? `hold-${press.kind}` : R.V.rec ? "rec" : "";
};
// hint in the empty field: voice is on and there is a keyboard (a phone has no hold-space recording) — so the space
// shortcut is advertised
export const spaceHint = () => (R.canRecord() && !matchMedia("(pointer: coarse)").matches ? " · " + t("web.voice.space_hint") : "");

// Dictation into a message field: one microphone per field (it survives the action icon being rebuilt in the middle of a
// recording). A failure goes to the field's error line, an untranscribed recording is kept there with "Retry"/"Download"
const dicts = new WeakMap();   // field → button
const keptDictation = {};      // field key → {retry, download, error}
const ownError = {};           // field key → the text this dictation put into the error line (and only that is its to clear)
// offered while the error on screen is this very dictation's
export const failedDictation = (key) => (keptDictation[key] && S.errors[key] === keptDictation[key].error ? keptDictation[key] : null);
function dictation(ta, key) {
  let d = dicts.get(ta);
  if (d) return d;
  d = dictateButton(ta, {
    space: false, bind: false,
    // idle with nothing to dictate into: the button is hidden (while it records, opens or recognizes it stays: it is the only stop)
    idleHide: () => !ta.value.trim() && !(S.pending[key] || []).length,
    // the line it reported is gone (a send cleared it, another error took its place): the button is not red any more
    errorShown: (text) => S.errors[key] === text,
  }, undefined, (text, actions) => {
    if (text) { S.errors[key] = text; ownError[key] = text; }
    else {
      // a successful dictation clears only its own error, not a failed send or voice message under the same field
      if (S.errors[key] === ownError[key]) S.errors[key] = "";
      delete ownError[key];
    }
    if (text && actions) keptDictation[key] = { ...actions, error: text }; else delete keptDictation[key];
  });
  dicts.set(ta, d);
  return d;
}

// the action buttons repaint on the recorder's tick too (the recording's field can leave the screen with no snapshot):
// one entry per field — WeakRefs of the fields, each field's latest button — since the button is rebuilt on every
// recording change. The dictation buttons have their own tick (dictate.js)
const fields = new Set();
const latest = new WeakMap();   // field → its action button
let painting = false;   // subscribe with the first button: at module load recorder.js may not be ready yet (import cycle)
const repaintAll = () => {
  for (const w of fields) {
    const ta = w.deref();
    if (!ta) { fields.delete(w); continue; }
    const b = latest.get(ta);
    if (b?.isConnected) b.hubPaintSelf();
  }
};
// a field on screen: in the page, with boxes, not visibility: hidden (every browser; checkVisibility's options are newer)
const onScreen = (n) => n.getClientRects().length > 0 && getComputedStyle(n).visibility !== "hidden";

// the right-hand icon: stop (recording) · send (there is content) · microphone (empty and voice is on) · send (no voice);
// b.hubDict — the dictation microphone beside it, shown while there is content (the composer puts it into the row).
// fillText(text) — where the words go when the recording was ended with "Done" (the strip's button): into the field, unsent.
// lock() — the field's send for the words of a recording, addressed at the moment it is called ("send" pressed): the
// reply target, the board cards, the files then in the field (composer.js lock); a field without it sends with sendText
export function actionButton(s, ta, sendText, fillText, lock = null) {
  const me = {
    key: s.name, field: ta, kind: "message", session: s.name, sendText, ta,
    empty: () => { const f = R.curField(); return (!f || !f.value.trim()) && !(S.pending[s.name] || []).length; },
    lock: () => lock?.() || null,
    deliver: (rec, send) => transcribeRecording(rec, send || sendText, fillText),
    say: (text) => { S.errors[s.name] = text; },
  };
  R.bind(ta, me);
  const cur = R.currentTarget();
  if (!cur || !cur.field?.isConnected || cur.field === $("msg") || !$("msg")) R.setCurrent(me);
  const hasContent = () => ta.value.trim() || (S.pending[s.name] || []).length;
  const b = el("button", "btn round");
  const dict = dictation(ta, s.name);
  b.hubDict = dict;
  let shown = null;
  // whose recording: one into another field that is on screen (a plugin's hub.input().record()) is that field's — it
  // shows its own strip and stop, this button stays a microphone, off until it ends (as the dictation button). One whose
  // field is gone or hidden (another session's, left by navigating; a plugin panel closed or removed mid-recording) keeps
  // this stop: it is the only control of the running microphone there. On screen is judged by the field's form: the
  // core hides the field itself while it records (.composer.rec .prompt)
  const foreign = () => {
    if (!R.recording()) return false;
    const at = R.V.target || R.currentTarget();
    if ((R.V.session || at?.key) === me.key || !at?.field || at.field === ta) return false;
    return onScreen(at.field.closest?.("form.composer") || at.field);
  };
  const mode = () => {
    const other = foreign(), own = live() && !other, rec = R.V.rec && own, press = R.pressed();
    return rec && press ? "hold" : rec ? "rec" : R.V.busy && own ? "busy" : R.V.opening && own ? "opening"
      : !hasContent() && R.voiceShown() ? (other ? "other" : "mic") : "send";
  };
  const paint = () => {
    dict.hubPaint();   // with an empty field and no dictation going the main microphone is the only one
    paintSelf();
  };
  const paintSelf = () => {
    // the icon changes only when the mode changes (it used to be redrawn on every keystroke and the sprite's <use>
    // flickered)
    const m = mode();
    if (m === shown) return;
    shown = m;
    b.replaceChildren();
    b.onclick = null;
    b.onpointerdown = null;
    b.disabled = false;   // the modes that need it off say so below ("other" left it on before a stop)
    if (m === "hold") {
      b.type = "button";
      b.className = "btn round stop";
      b.append(icon("mic"));
      b.setAttribute("aria-label", t("web.voice.release_to_send"));
      b.onclick = () => R.stop();
    } else if (m === "rec") {
      b.type = "button";
      b.className = "btn round stop";
      b.append(icon("stop"));
      b.setAttribute("aria-label", t("web.voice.stop_send"));
      b.onclick = () => R.V.rec?.stop();
    } else if (m === "busy" || m === "opening") {
      b.type = "button";
      b.className = "btn round";
      b.disabled = true;
      b.append(el("span", "spin-dot"));
      b.setAttribute("aria-label", m === "busy" ? t("web.voice.recognizing") : t("web.voice.opening_mic"));
    } else if (m === "mic" || m === "other") {
      b.type = "button";
      b.className = "btn round mic";
      b.disabled = m === "other";   // another field on the page is recording: a press here would do nothing
      b.append(icon("mic"));
      b.setAttribute("aria-label", t("web.voice.voice_message"));
      b.title = R.blocked() ? t(R.blocked()) : t("web.voice.voice_title");   // cannot record — the hint says why
      b.onpointerdown = (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        if (R.recording()) return;   // a dictation into another field is recording: the press would only take over its target
        R.setCurrent(me);
        R.pressStart("pointer");
        R.holdPointer(e.pointerId);   // releasing a different finger does not send the recording
      };
    } else {
      b.type = "submit";
      b.className = "btn round send";
      b.disabled = false;
      b.append(icon("send"));
      b.setAttribute("aria-label", t("web.voice.send"));
      b.title = t("web.voice.send_title");
    }
  };
  b.hubRecord = () => R.startFor(me);   // hub.input().record(): the same start as the microphone button, from code
  b.hubPaint = paint;   // a draft set by code (setDraft) repaints from outside, without an input event
  b.hubPaintSelf = paintSelf;
  paint();
  if (!latest.has(ta)) fields.add(new WeakRef(ta));
  latest.set(ta, b);
  if (!painting) { painting = true; R.onChange(repaintAll); }
  ta.addEventListener("input", paint);
  return b;
}

// the recording strip — instead of the input field while recording or recognizing
// fillText — the field takes words without sending them: the "Done" button is there only when it can
export function recStrip(key, fillText) {
  if (!recording() || !live()) return null;
  if (key && (R.V.session || R.currentTarget()?.key) !== key) return null;   // recording into another field (a session's or a plugin's)
  const strip = el("div", "recstrip");
  // × — cancel without sending (Esc does the same); also while the speech is recognized: the answer is then dropped
  const cancel = el("button", "btn round quiet rec-cancel");
  cancel.type = "button";
  cancel.append(icon("deny"));
  cancel.setAttribute("aria-label", t("web.voice.cancel"));
  cancel.title = t("web.voice.cancel");
  cancel.addEventListener("click", R.cancel);
  if (R.V.busy) {   // the length of the recording stays on screen (no data-rec-clock: it does not tick)
    strip.append(el("span", "spin-dot"), el("span", "t", R.fmt(R.V.took)), el("span", "t rec-wait", t("web.voice.recognizing_dots")), el("span", "spacer"), cancel);
    return strip;
  }
  if (R.V.opening) {   // the press is accepted, the browser is opening the microphone: visible at once, not when (and if) it opens
    strip.append(el("span", "spin-dot"), el("span", "t", t("web.voice.opening_mic_dots")), el("span", "spacer"), cancel);
    return strip;
  }
  const tick = el("span", "t", R.clock());
  tick.dataset.recClock = "1";
  const press = R.pressed();
  if (press) {  // the button or Space is held — releasing means sending
    strip.append(el("span", "rec-dot"), tick, el("span", "spacer"),
      el("span", "hint", press.kind === "space" ? t("web.voice.release_space") : t("web.voice.release_to_send")), cancel);
    return strip;
  }
  // "Done" — stop and put the words into the field to look them over or add to them; nothing is sent
  const done = fillText ? el("button", "btn small rec-done") : null;
  if (done) {
    done.type = "button";
    done.append(icon("allow"), document.createTextNode(t("web.voice.done")));
    done.title = t("web.voice.done_title");
    done.addEventListener("click", R.stopInto);
  }
  strip.append(el("span", "rec-dot"), tick, el("span", "spacer"), ...(done ? [done] : []), cancel);
  return strip;
}

// the recording is kept until it is transcribed (a long voice message used to be lost after a recognition error): "Retry" and "Download"
const failed = {};   // field key → {data, mime, blob, session}
// offered while the error on screen belongs to this very recording: "no speech in the recording" from the next one is not about it
export const failedRecording = (key) => (failed[key] && S.errors[key] === failed[key].error ? failed[key] : null);

// rec.insert — it ended with "Done": the words go to fillText (the field), not to sendText. sendText addressed at "send"
// (sendText.session — composer.js lock) shows the message in that session's feed meanwhile and gives the field its reply
// and cards back when nothing is sent (sendText.drop)
export async function transcribeRecording(rec, sendText, fillText) {
  R.V.target = null;   // a voice message (and "Retry" too): the session field gets the focus after the recording
  const cid = sendText.session ? hold(sendText.session) : null;   // a plugin's field has no feed
  const drop = () => { settled(cid); sendText.drop?.(); };
  const res = await R.recognize(rec);
  if (res.discarded) { drop(); return R.redraw(); }   // "Cancel" while it was recognized: nothing is sent, nothing is shown, the recording is let go
  // 422 — no speech: a retry would hear the same, so we do not offer "Retry" and do not displace the earlier failed one
  if (R.noSpeech(res)) { drop(); S.errors[rec.session] = res.error; return R.redraw(); }
  if (res.error) {
    drop();   // the reply and the cards are back over the field: a message typed by hand now goes with them
    const why = R.failureText(res);
    failed[rec.session] = { ...rec, sendText, fillText, error: why };   // "Retry" of a voice message addresses it anew (composer.js lock)
    S.errors[rec.session] = why;
    return R.redraw();
  }
  delete failed[rec.session];
  S.errors[rec.session] = "";
  R.redraw();
  if (rec.insert && fillText) { drop(); fillText(res.data.text); return R.redraw(); }
  const text = `🎤 ${t("web.voice.head")}: ${res.data.text}`;
  fill(cid, text);
  sendText(text, cid);  // right away, like a Telegram voice message; the core parses any word after the mic (core/forms.py HEADS)
  release(cid);
}

// "Retry" and "Download" after the text about an untranscribed recording: the voice message and dictation into the page's
// fields (dictate.js fieldMic)
export function keptLinks({ retry, download }) {
  const again = el("button", "linkbtn", t("web.composer.retry_transcribe"));
  again.type = "button";
  again.addEventListener("click", retry);
  const save = el("button", "linkbtn", t("web.composer.save_recording"));
  save.type = "button";
  save.addEventListener("click", download);
  return [" ", again, " ", save];
}

export function retryDownload(rec) {
  const a = el("a");
  a.href = URL.createObjectURL(rec.blob);
  a.download = "voice" + (rec.mime.includes("ogg") ? ".ogg" : rec.mime.includes("mp4") ? ".mp4" : ".webm");
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
