// Dictation into a field (Plugin API 1.31: voice wherever there is input): hub.dictate(field, opts) is a microphone
// button for any field: input, textarea or contenteditable. Recording uses the same mechanism as the voice message
// (recorder.js: microphone permission, hold — speak — release, a click starts recording and another click stops it,
// Space in an empty field, Esc cancels); the transcription goes into the field at the caret (replacing the selection),
// and the field gets an input event. Nothing is sent, like dictation in native clients. If voice is not configured or
// the browser gives no microphone, the button is hidden.
// opts: onText(text, { inserted }) — transcribed: inserted means it went into the field, false means the field was
// rebuilt while recognition ran or became read-only or disabled (the text is handed to the plugin instead of being lost); onError(text, { retry,
// download }) — what to tell the owner (also the button's tooltip); retry/download are there when the recording was not
// transcribed and is kept for a retry; retry does nothing while another recording runs. Callbacks run in try, like other
// Plugin API callbacks: a throw does not break the recording.
// Space starts recording only while the caret is in the field itself (space: false — not even then, the button only).
// bind: false — the field already has its own target (the page's message field: Space in it records a voice message, not
// dictation) and the button is only a second way to speak into it.
import { $, el, icon } from "./dom.js";
import { t } from "./i18n.js";
import * as R from "./recorder.js";
import { keptLinks, retryDownload } from "./voice.js";
import { banner } from "./api.js";

const buttons = new Set();   // WeakRefs of the buttons: repaint on a recording change and on each tick (voice turned on later)
let painting = false;   // subscribe with the first button: at module load recorder.js may not be ready yet (import cycle)
const repaint = () => { for (const w of buttons) { const b = w.deref(); if (b) b.hubPaint(); else buttons.delete(w); } };

const INPUTS = /^(text|search|url|tel|email)$/;
export const dictable = (n) => n instanceof HTMLElement
  && (n.isContentEditable || n instanceof HTMLTextAreaElement || (n instanceof HTMLInputElement && INPUTS.test(n.type)));
const textOf = (n) => (n.isContentEditable ? n.textContent : n.value) || "";
// nobody can edit the field now: read-only or disabled (a frozen card), or a contenteditable that stopped being one
const locked = (n) => n.readOnly || n.disabled
  || (!(n instanceof HTMLInputElement || n instanceof HTMLTextAreaElement) && n.isConnected && !n.isContentEditable);   // detached: not known yet

// the field's caret: [start, end] for input/textarea, a Range for contenteditable; field not focused — null (insert at the end)
function caretOf(field) {
  if (document.activeElement !== field) return null;
  if (!field.isContentEditable) return field.selectionStart == null ? null : [field.selectionStart, field.selectionEnd];
  const sel = getSelection();
  return sel.rangeCount && field.contains(sel.getRangeAt(0).startContainer) ? sel.getRangeAt(0).cloneRange() : null;
}

// put the caret back (the saved one or at the end) and return the characters on both sides — for spaces around the words
function restore(field, caret) {
  field.focus({ preventScroll: true });
  if (!field.isContentEditable) {
    const n = field.value.length;
    if (field.selectionStart == null) return [field.value.slice(-1), ""];
    const [s, e] = caret ? [Math.min(caret[0], n), Math.min(caret[1], n)] : [n, n];
    field.setSelectionRange(s, e);
    return [field.value[s - 1] || "", field.value[e] || ""];
  }
  let r = caret && field.contains(caret.startContainer) ? caret : null;
  if (!r) { r = document.createRange(); r.selectNodeContents(field); r.collapse(false); }
  const sel = getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  const before = document.createRange(), after = document.createRange();
  before.selectNodeContents(field); before.setEnd(r.startContainer, r.startOffset);
  after.selectNodeContents(field); after.setStart(r.endContainer, r.endOffset);
  return [before.toString().slice(-1), after.toString().slice(0, 1)];
}

// fail(what, err) — a plugin callback threw: report it in Plugins (ext.js), the recording goes on; show(text, actions, said) —
// the page's own fields (fieldMic): every change of what to tell the owner ("" clears it) goes there instead of onError
export function dictateButton(field, opts = {}, fail = (what, err) => console.warn(what, err), show = null) {
  if (!dictable(field)) throw new TypeError("hub.dictate: input, textarea or contenteditable");
  const call = (name, ...args) => {
    if (typeof opts[name] !== "function") return false;
    try { opts[name](...args); } catch (err) { fail(name, err); }
    return true;
  };
  const b = el("button", "btn round mic dictate");
  b.type = "button";
  let error = "", caret = null, shown = null, words = "";
  // tell the owner: kept is the recording this very text is about (not transcribed, kept for a retry), said — the
  // transcription that was not inserted; "Retry" while another recording runs does nothing: recognition would take over
  // the shared recording and its view
  const tell = (text, kept = null, said = "") => {
    const actions = kept ? { retry: () => { if (!R.recording()) again(kept); }, download: () => retryDownload(kept) } : null;
    if (show) show(text, actions, said);
    else if (text) call("onError", text, actions || {});
  };
  // the error of this field: the button turns red with it in the tooltip
  const report = (text, kept = null) => {
    error = text || "";
    tell(error, kept);
    b.hubPaint();
  };
  const me = {
    key: `dictate:${Math.random().toString(36).slice(2, 10)}`, field, kind: "dictate", spaceStops: false,
    spaceStarts: opts.space !== false,
    empty: () => !locked(field) && !textOf(field).trim(),
    begin: () => {
      // the caret in the field now (it may be moved during the recording — then the new one is taken)
      caret = caretOf(field);
    },
    say: (text) => report(text),
    deliver: (rec) => again(rec),
  };
  async function again(rec) {
    R.V.target = me;
    const res = await R.recognize(rec);
    if (res.discarded) return R.redraw();
    // 422 — no speech in this recording: a retry would hear the same, and an earlier failed recording is not offered
    // under the message about this one (the plugin keeps the retry it was given with that earlier error)
    if (res.error) { report(R.failureText(res), R.noSpeech(res) ? null : rec); return R.redraw(); }
    report("");
    words = String(res.data?.text || "").trim();
    R.redraw();   // settle inserts after the redraw: it may rebuild the plugin field, and the focus would be lost
  }
  // the recording ended: the transcription goes into the field at the caret (if the caret moved during the recording — there)
  me.settle = () => {
    if (!words) return;
    const said = words;
    words = "";
    // the field was rebuilt during recognition, or it became read-only (a card frozen mid-recording): the text goes to
    // the plugin or is shown to the owner, not into the void and not into a field nobody can edit or send from
    if (!field.isConnected || locked(field)) {
      if (!call("onText", said, { inserted: false })) tell(t("web.voice.dictate_lost", { text: said }), null, said);
      return;
    }
    const at = document.activeElement === field ? caretOf(field) : caret;
    const [prev, next] = restore(field, at);
    R.typeText(field, (prev && !/\s/.test(prev) ? " " : "") + said + (next && /[\p{L}\p{N}]/u.test(next) ? " " : ""));
    call("onText", said, { inserted: true });
  };
  if (opts.bind !== false) R.bind(field, me);

  const mode = () => {
    if (!R.canRecord()) return "off";
    const mine = R.V.target === me;
    // a read-only (frozen card) or disabled field has no microphone; a recording into it runs to the end
    if (locked(field) && !(mine && R.recording())) return "off";
    if (R.recording() && !mine) return "other";   // recording into another field
    if (mine && R.V.busy) return "busy";
    if (mine && R.V.opening) return "opening";
    if (mine && R.V.rec) return R.pressed() ? "hold" : "rec";
    return error ? "err" : "mic";
  };
  const LABEL = { mic: "web.voice.dictate", other: "web.voice.dictate", opening: "web.voice.opening_mic",
    busy: "web.voice.recognizing", rec: "web.voice.stop_insert", hold: "web.voice.release_to_insert" };
  b.hubPaint = () => {
    // opts.errorShown(text): the line the error was shown in is gone, so the button forgets it; opts.idleHide(): hidden
    // while idle (not while it records, opens or recognizes: then it is the only way to stop)
    if (error && opts.errorShown && !opts.errorShown(error)) error = "";
    const m = mode(), sig = m + "|" + error;
    b.style.display = opts.idleHide?.() && m !== "rec" && m !== "hold" && m !== "busy" && m !== "opening" ? "none" : "";
    if (sig === shown) return;
    shown = sig;
    b.hidden = m === "off";
    b.disabled = m === "busy" || m === "other";
    // beside the message field it is not the page's microphone (.mic: themes and tests address that one), a quiet round button
    const idle = opts.bind === false ? "quiet" : "mic";
    b.className = "btn round dictate " + (m === "rec" || m === "hold" ? "stop" : m === "err" ? idle + " err" : idle) + (b.dataset.mini ? " mini" : "");
    b.replaceChildren(m === "busy" || m === "opening" ? el("span", "spin-dot") : icon(m === "rec" ? "stop" : "mic"));
    const label = m === "err" ? error : t(LABEL[m] || "web.voice.dictate");
    b.setAttribute("aria-label", label);
    // Space is advertised only where it starts recording (space: false — the button only)
    b.title = m === "mic" ? t(me.spaceStarts ? "web.voice.dictate_title" : "web.voice.dictate_title_button") : label;
  };
  // the press is on pointerdown (focus and caret stay in the field): hold — speak — release; a click starts recording,
  // another click inserts; while the microphone is opening — cancel
  b.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const m = mode();
    if (m === "rec" || m === "hold") return R.stop();
    if (m === "opening") return R.cancel();
    if (m !== "mic" && m !== "err") return;
    R.setCurrent(me);
    R.pressStart("pointer");
    R.holdPointer(e.pointerId);   // the release is caught on the document; another finger does not end it
  });
  b.addEventListener("mousedown", (e) => e.preventDefault());   // with the mouse too: the focus stays in the field
  // from the keyboard (Enter or Space on the button) — like a click: record, again — insert
  b.addEventListener("click", (e) => {
    if (e.detail !== 0) return;
    const m = mode();
    if (m === "rec" || m === "hold") return R.stop();
    if (m === "opening") return R.cancel();
    if (m !== "mic" && m !== "err") return;
    R.setCurrent(me);
    R.pressStart("pointer");
    R.pressEnd();
  });
  b.hubPaint();
  buttons.add(new WeakRef(b));
  if (!painting) { painting = true; R.onChange(repaint); }
  return b;
}

// The microphone at the page's own fields: a card's "own answer" and the feed search (owner decision 2026-10-26, review
// #294): one button per field, a redraw reuses it. A failure is the line b.hubNote, which the caller puts under the field:
// an untranscribed recording is kept there with "Retry" and "Download", as for the voice message; the field is gone (the
// search closed, the card went away) — the same at the top of the page. The press does not reach the row under the
// button. opts.space: false — Space in the field does not start recording (search: the button only).
const mics = new WeakMap();
// a dictation into a field inside node is recording or recognizing, or its failure (a kept recording, the text that was
// not inserted) is shown under the field: a card with it does not just go away when its question closes — it freezes
export const dictationIn = (node) => (R.recording() && !!R.V.target && node.contains(R.V.target.field))
  || !!node.querySelector(".dictate-note[data-keep]");
export function fieldMic(field, opts = {}) {
  let b = mics.get(field);
  if (b) return b;
  const note = el("p", "dictate-note");   // not .error-text: the card finds its own error line by that class
  note.setAttribute("role", "alert");
  let said = null;   // the banner's text node while the banner speaks for this field
  // data-keep — the line holds something to keep (a recording, a transcription): the card freezes rather than going
  // away (dictationIn); data-said — the transcription itself: "To the input field" of a frozen card takes it (dock.js)
  const show = (text, actions, words) => {
    const top = $("banner");
    if (said && top.firstChild === said) banner("");   // what the banner said for this field is outdated
    said = null;
    const links = actions ? keptLinks(actions) : [];
    delete note.dataset.keep;
    delete note.dataset.said;
    if (!text || note.isConnected) {
      note.replaceChildren(...(text ? [text, ...links] : []));
      if (text && (actions || words)) note.dataset.keep = "1";
      if (text && words) note.dataset.said = words;
      return;
    }
    note.replaceChildren();   // the field is gone: the line would come back stale with it (the search reopens)
    banner(text);   // with the links the banner does not fade by itself (its text is no longer the same)
    said = top.firstChild;
    // a click on the banner closes it: not on the links — "Download" and a "Retry" refused during another recording
    // would take the only way to the recording with them
    for (const n of links) n.addEventListener?.("click", (e) => e.stopPropagation());
    top.append(...links);
  };
  b = dictateButton(field, opts, undefined, show);
  b.hubNote = note;
  b.dataset.mini = "1";
  b.classList.add("mini");
  for (const ev of ["pointerdown", "click"]) b.addEventListener(ev, (e) => e.stopPropagation());
  mics.set(field, b);
  return b;
}
