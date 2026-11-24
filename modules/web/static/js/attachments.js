// Attachments: the "File" button, drag-and-drop onto the feed, pasting an image from the clipboard (⌘V). A file is uploaded to the server right away.
import { $ } from "./dom.js";
import { S, render } from "./state.js";
import { post } from "./api.js";
import { boxes, refreshBoxes } from "./boxes.js";
import { t } from "./i18n.js";
import { picker } from "./recorder.js";

const taOf = (name) => boxes.get(name)?.ta || $("msg");   // a plugin has its own field, the session field is #msg
const redraw = () => { render(); refreshBoxes(); };

let seq = 0;

// An image is a "[Image #N]" mark in the text at the cursor, as in Claude Code: the session sees
// which image is meant; the number is the next one after those already attached to this message.
const MARK = (label) => `[${label}]`;
function insertMark(name, label) {
  const ta = taOf(name);
  const text = S.drafts[name] || "";
  const at = ta && ta.value === text && document.activeElement === ta ? ta.selectionStart : text.length;
  const before = text.slice(0, at), after = text.slice(at);
  const ins = (before && !/\s$/.test(before) ? " " : "") + MARK(label) + (after && !/^\s/.test(after) ? " " : "");
  S.drafts[name] = before + ins + after;
  if (ta) { ta.value = S.drafts[name]; ta.selectionStart = ta.selectionEnd = at + ins.length; }
}

// the text without a file's "[Image #N]" mark
export const withoutMark = (text, p) => (p.label ? text.replace(MARK(p.label) + " ", "").replace(MARK(p.label), "") : text);

export function dropMark(name, p) {
  if (!p.label) return;
  S.drafts[name] = withoutMark(S.drafts[name] || "", p);
  const ta = taOf(name);
  if (ta) ta.value = S.drafts[name];
}

// The browser hands a folder over as a file with no content: Safari with size 0, Chrome with the folder's size on disk,
// which cannot be read. A folder dropped onto the page is recognized by webkitGetAsEntry — only during the drop/paste event.
const folders = new WeakSet();
export function droppedFiles(dt) {
  const files = [...(dt?.files || [])];
  [...(dt?.items || [])].filter((it) => it.kind === "file").forEach((it, i) => {
    if (files[i] && it.webkitGetAsEntry?.()?.isDirectory) folders.add(files[i]);
  });
  return files;
}

// Without that marker (pasting from the clipboard) a folder shows up as a read error: in Safari on reading the whole
// file (size 0, nothing to read), in Chrome already on the first byte.
const readable = (f) => new Promise((done) => {
  const reader = new FileReader();
  reader.onload = () => done(true);
  reader.onerror = () => done(false);
  reader.readAsArrayBuffer(f.size ? f.slice(0, 1) : f);
});

// The file goes to the hub as a raw stream: the request body is the file itself, the browser sends it from disk, up to
// the hub's limit (the snapshot's upload field). An older hub does not announce this — base64 in JSON, as before (up to 20 MB).
async function upload(f, name, mime) {
  if (!(await readable(f))) return { error: t("web.attachments.unreadable") };
  const up = S.snap?.upload;
  if (!up?.raw) {
    const data = await new Promise((done) => {
      const reader = new FileReader();
      reader.onload = () => done(String(reader.result).split(",")[1] || "");
      reader.onerror = () => done("");
      reader.readAsDataURL(f);
    });
    return post("/api/upload", { name, mime, data });
  }
  if (up.max_bytes && f.size > up.max_bytes) return { error: t("web.attachments.too_big", { mb: Math.floor(up.max_bytes / 1048576) }) };
  return post(`${up.raw}?name=${encodeURIComponent(name)}&mime=${encodeURIComponent(mime)}`, f, mime);
}

export function addFiles(name, files) {
  const dirs = files.filter((f) => folders.has(f));
  const note = dirs.length ? t("web.attachments.folder", { name: dirs.map((f) => f.name).join(", ") }) : "";
  if (note) S.errors[name] = note;
  for (const f of files.filter((f) => !folders.has(f))) {
    const image = f.type.startsWith("image/");
    const list = (S.pending[name] = S.pending[name] || []);
    const n = image ? 1 + Math.max(0, ...list.map((q) => Number((q.label || "").split("#")[1]) || 0)) : 0;
    const p = { key: ++seq, name: f.name || (image ? t("web.attachments.image_name") : t("web.attachments.file_name")), size: f.size,
                meta: null, preview: image ? URL.createObjectURL(f) : null, label: image ? `Image #${n}` : null, text: f.pasted || null };
    list.push(p);
    boxes.get(name)?.flash?.(p.name);   // briefly highlight the field
    if (p.label) insertMark(name, p.label);
    upload(f, p.name, f.type || "application/octet-stream").then((res) => {
      if (res.error) {
        p.failed = `${p.name}: ${res.error}`;   // a voice message waiting for this file keeps its words in the field (next to this) instead of going out without it
        S.pending[name] = (S.pending[name] || []).filter((q) => q !== p);
        dropMark(name, p);
        S.errors[name] = p.failed;
      } else {
        p.meta = p.label ? { ...res.data, label: p.label } : res.data;
        S.errors[name] = note;
      }
      redraw();
    });
  }
  redraw();
}

// A long paste (a log, an article) is a text attachment, not a wall of text in the field — as in the phone and Mac apps:
// only text pasted at once, longer than 20000 characters or 400 lines; what is typed or dictated is never folded. The
// file goes the raw way like any other (no length limit of its own); its chip shows the start of the text in a small tile.
const PASTE_CHARS = 20000, PASTE_LINES = 400;
export const longPaste = (s) => s.length > PASTE_CHARS || s.trim().split("\n").length > PASTE_LINES;
export function pasteTile(name, text) {
  const used = (S.pending[name] || []).map((q) => Number((/^pasted-(\d+)\.txt$/.exec(q.name) || [])[1]) || 0);
  const f = new File([text], `pasted-${1 + Math.max(0, ...used)}.txt`, { type: "text/plain" });
  f.pasted = text.slice(0, 600);
  addFiles(name, [f]);
}

export function pickFiles(name) {
  const input = $("file-input");
  input.dataset.for = name;
  if (recordingBox() === name) {   // a screenshot for the voice message: the recording does not stop on the quiet while the picker is open
    // closed: a file chosen, "cancel", or the window has the focus back (a browser without "cancel" — before 2023)
    const ends = [[input, "change"], [input, "cancel"], [window, "focus"]];
    const done = () => { picker(false); ends.forEach(([on, ev]) => on.removeEventListener(ev, done)); };
    picker(true);
    ends.forEach(([on, ev]) => on.addEventListener(ev, done));
  }
  input.click();
}

// A voice message is recording: the field is hidden, so ⌘V lands on the page, not in the field. A file from the clipboard
// goes to the recording field and rides out with the voice message: it is there before "send" is pressed (typing into
// another field keeps its own paste).
export function recordingBox() {
  for (const [name, b] of boxes) if (b.files !== false && b.ta.isConnected && b.ta.closest("form.composer.rec")) return name;
  return null;
}

export function bindAttachments() {
  document.addEventListener("paste", (e) => {
    if (e.defaultPrevented) return;
    const name = recordingBox();
    if (!name) return;
    const at = e.target;
    if (at !== boxes.get(name).ta && at?.closest?.("input, textarea, [contenteditable]:not([contenteditable=false])")) return;
    const files = droppedFiles(e.clipboardData);
    if (!files.length) return;
    e.preventDefault();
    addFiles(name, files);
  });
  $("file-input").addEventListener("change", (e) => {
    const n = e.target.dataset.for;
    if (n) addFiles(n, [...e.target.files]);
    e.target.value = "";
  });
  const main = $("main");
  main.addEventListener("dragover", (e) => {
    if (S.cur && e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); main.classList.add("dragging"); }
  });
  main.addEventListener("dragleave", (e) => { if (!main.contains(e.relatedTarget)) main.classList.remove("dragging"); });
  main.addEventListener("drop", (e) => {
    main.classList.remove("dragging");
    if (!S.cur || !e.dataTransfer?.files.length) return;
    e.preventDefault();
    addFiles(S.cur.name, droppedFiles(e.dataTransfer));
  });
}
