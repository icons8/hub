// Input field: one build shared by the session field (dock.js) and plugin fields (inputbox.js, API 1.7), no libraries.
// State stays in S (S.drafts, S.pending, S.errors): attachments.js, voice.js and main.js read it. The form and its
// parts are built once and then only updated — the same nodes between snapshots, so the action icon does not flicker
// and focus and the caret are not lost. Attachments and voice ask to redraw the field by draft key via boxes.
// Voice (actionButton, recStrip) uses ready-made nodes from voice.js: rebuilt only when the mode changes (modeSig).
import { el, icon, kit, lookSig } from "./dom.js";
import { S, render, sessionName } from "./state.js";
import { awaitDecision, newClientId, sendMessage } from "./api.js";
import { unsent } from "./drafts.js";
import { addFiles, chipNodes, droppedFiles, longPaste, pasteTile, pickFiles, withoutMark } from "./attachments.js";
import { actionButton, failedDictation, failedRecording, keptLinks, recording, recStrip, retryDownload, transcribeRecording, voiceMode } from "./voice.js";
import { boxes } from "./boxes.js";
import { resolve, run as runCommand, suggest } from "./commands.js";
import { t } from "./i18n.js";
import { composerLayout } from "./composer-layout.js";
import { claim, sent, settled, typed } from "./outgoing.js";
import { busLinkIn, pasteIntoHubs } from "./hubs.js";

// a link of the hub bus typed or pasted into a session's field (spec hubbus/ux §3.3 p. 5): it is a secret — a session's
// transcript keeps what it gets, so the page does not send it until the owner says «send as text»
const linkHeld = new Set(), asText = new Map();   // field key -> held now; field key -> the text allowed as text

// hint for the "focus the input field" shortcut: ⌘I on Mac, Ctrl I elsewhere
const MAC = /mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform || "");
const HOTKEY = MAC ? "⌘I" : "Ctrl I";

// brings parent's children to the kids list without touching nodes already in place: a removed and re-added node loses focus
function sync(parent, kids) {
  for (const n of [...parent.children]) if (!kids.includes(n)) n.remove();
  kids.forEach((n, i) => { if (parent.children[i] !== n) parent.insertBefore(n, parent.children[i] || null); });
}
const kitSig = (name) => name + (kit.icons[name] || "");   // a theme may have its own icon
// the field is redrawn on every snapshot: a write of the same text or attribute is still a change of the page (a theme's
// observer wakes, the row is laid out again) — only what differs is written
const setText = (n, v) => { if (n.textContent !== v) n.textContent = v; };
const setAttr = (n, name, v) => { if (n.getAttribute(name) !== v) n.setAttribute(name, v); };

// Unknown send outcome (API 1.28): by draft key — what was sent and with which client_id. The owner sends the same
// thing again (same recipient, text, files, quote and reply-to) — with the same id, and the core will not duplicate
// the message if the first one did arrive; a different quote or reply-to with the old id would be taken by the core
// for a repeat and silently not delivered. wake does not count: the first one that arrived woke the session, the
// retry goes without it. The record is kept across a reload together with the draft (drafts.js: unsent, key -> {sig, client_id})
// the core's refusal reason, with a trailing period added if missing: it is followed by the "Text and files came back…" sentence
const sentence = (s) => (/[.!?…]$/.test(s.trim()) ? s : s.trim() + ".");
const sendSig = (b) => JSON.stringify([b.session, b.text, (b.attachments || []).map((f) => f?.path), b.quote ?? null,
  b.reply_to ?? null, (b.tasks || []).map((x) => x.id)]);

// files still uploading in a field (only — just these of them): resolves once each has its answer (a failed one drops out
// of the list), at most UPLOAD_WAIT
const UPLOAD_WAIT = 60000;
const uploaded = (key, only = null) => new Promise((done) => {
  const until = Date.now() + UPLOAD_WAIT;
  const busy = () => (S.pending[key] || []).some((p) => !p.meta && (!only || only.includes(p)));
  const tick = () => (busy() && Date.now() < until ? setTimeout(tick, 200) : done());
  tick();
});

// props: key (draft key), to, id, label, placeholder, hint, cls, hotkey, files (false — no attachments: no paperclip, paste or drop of files), reply {text, cancel}, leading() + leadingSig,
// body(words, files, aim?) → /api/send body, aim() → what a message is addressed with right now (a reply target, board
// cards: a voice message takes it when "send" is pressed, see lock below), send(body) → receipt (a plugin's own transport, API 1.28; without it — sendMessage),
// holdLinks (a hub bus link is held with send too: the words still reach a session),
// before(body), failed(body), restore(body) (what rode along with the words besides the attachments goes back to its place:
// board cards), sent({text, to}, flash), tray (a node with chips above the field, it is shown while it has content); commands — the session name for which
// the field understands commands via "/" (js/commands.js): a hint while typing; core and plugin commands — without a session.
// Returns {node, update(next), refresh(), focus(), ta(), draft(), setDraft({text?, files?})}: node is the form itself, callers keep it between snapshots
export function buildComposer(props) {
  const o = { ...props };
  const key = o.key;
  S.pending[key] = S.pending[key] || [];

  const form = el("form", "composer" + (o.cls ? " " + o.cls : ""));
  form.dataset.drop = t("web.index.drop");   // the caption while a file is dragged over (css: attr(data-drop))
  const attach = el("button", "btn round quiet attach");
  attach.type = "button";
  attach.title = t("web.composer.attach_title");
  attach.setAttribute("aria-label", t("web.composer.attach_label"));
  attach.addEventListener("click", () => pickFiles(key));
  let attachIcon = "", closeIcon = "";

  let hk = null;
  if (o.hotkey) {
    hk = el("kbd", "kbd-hint", HOTKEY);
    hk.setAttribute("aria-hidden", "true");
    hk.title = t("web.composer.hotkey_title");
  }
  const toLine = el("div", "to-line");
  const toName = el("b");
  // × — the message is addressed away from the field's own session (a reply, a card): one press takes it off, as the reply bar's
  // "Cancel" does; with nothing to take off it is not there
  const toClear = el("button", "to-x");
  toClear.type = "button";
  let clearIcon = "";
  toClear.addEventListener("click", () => o.reply?.cancel());
  toLine.append(el("span", "to-dot"), t("web.composer.to") + " ", toName, toClear);
  // the reply: a card over the field — the quote in up to two lines (a title line above it when the words go to another
  // session), a round ✕ in its corner, as in the phone and Mac apps; a card aim or a note is one line in the same card
  const replyBar = el("div", "bar reply-bar");
  const replyChip = el("span", "chip warn reply-chip");
  const replyCancel = el("button", "x");
  replyCancel.type = "button";
  replyCancel.setAttribute("aria-label", t("web.composer.cancel"));
  replyCancel.addEventListener("click", () => o.reply?.cancel());
  replyBar.append(replyChip, replyCancel);
  const chips = el("div", "pending");
  let chipsSig = null;

  const prompt = el("div", "prompt");
  const glyph = el("span", "g", "❯");
  glyph.setAttribute("aria-hidden", "true");
  const ta = el("textarea");
  ta.rows = 1;
  let label = null;
  if (o.id) {
    ta.id = o.id;
    label = el("label", "sr");
    label.htmlFor = o.id;
  }
  if (o.hotkey) ta.setAttribute("aria-keyshortcuts", "Meta+I Control+I");
  if (o.commands) { ta.setAttribute("role", "combobox"); ta.setAttribute("aria-autocomplete", "list"); ta.setAttribute("aria-controls", (o.id || key) + "-cmds"); }
  const errId = (o.id || key) + "-err";
  ta.setAttribute("aria-describedby", errId);
  prompt.append(glyph, ...(label ? [label] : []), ta);

  const bar = el("div", "bar");
  const hint = el("span", "hint");
  const spacer = el("span", "spacer");
  const status = el("span", "sr");   // the same as the highlight, in words for screen readers
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  // the outcome of a typed command: a line of its own (the hint under the field is hidden on a phone and in Clean); always in
  // the form, hidden while empty, so a screen reader announces the text that comes into a live region that was already there
  const note = el("p", "cmd-answer");
  note.hidden = true;
  note.setAttribute("role", "status");
  const err = el("p", "error-text");
  err.id = errId;
  err.setAttribute("role", "alert");
  // command window above the field: open while "/name" is typed with no space and there is something to suggest
  const menu = el("div", "cmd-menu");
  menu.id = (o.id || key) + "-cmds";
  menu.setAttribute("role", "listbox");
  menu.setAttribute("aria-label", t("web.composer.commands"));
  menu.addEventListener("mousedown", (e) => e.preventDefault());   // a click on a row does not take focus away from the field
  let found = [], pick = 0, menuFor = null;   // menuFor — the text the window was built for
  // "To", the reply bar and attachment chips are in the form only when needed; other nodes stay in place
  const layout = () => {
    const kids = [...(o.files === false ? [] : [attach]), ...(hk ? [hk] : []), ...(o.to ? [toLine] : []), ...(o.reply ? [replyBar] : []),
      ...(S.pending[key].length ? [chips] : []), ...(o.tray?.childNodes.length ? [o.tray] : []), prompt, bar, status, note, err,
      ...(found.length ? [menu] : [])];
    sync(form, kids);
  };

  // grows with the text, up to 40% of the window; while the node is not on the page its height is unknown — leave it alone (else "0px")
  // measuring forces a layout of the whole page, and draw() runs on every snapshot: measure only when the text, the window
  // height or the field width (ResizeObserver below) changed; a theme's own layout (--composer-wrap) measures every time
  const textLayout = composerLayout(form, ta, attach, bar);
  let sized = "";
  const grow = (force) => {
    if (!ta.isConnected) { sized = ""; return; }
    const measured = textLayout.measure(() => grow(true));
    if (measured !== null) { sized = ""; ta.style.height = measured + "px"; return; }
    const sig = ta.value + "\0" + innerHeight;
    if (force !== true && sig === sized) return;
    sized = sig;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, innerHeight * 0.4) + "px";
  };

  // the message at the right of the hint ("Sent …"), the highlight and the text for screen readers: all clear themselves
  // waits — sends awaiting a receipt (API 1.29); stuckAny — at least one ended up stuck in the outbox
  let said = "", litTimer = 0, saidTimer = 0, waits = 0, stuckAny = false;
  // answer — the outcome of a typed command, shown in its own line (note): the person who typed it waits for the word
  let answer = false, answerNo = 0;
  const setFlash = (text, isAnswer) => { said = text || ""; answer = !!(said && isAnswer); answerNo += 1; draw(); };
  const flash = (name) => {
    clearTimeout(litTimer); clearTimeout(saidTimer);
    form.classList.add("flash");
    status.textContent = t("web.composer.added", { name });
    litTimer = setTimeout(() => form.classList.remove("flash"), 700);
    saidTimer = setTimeout(() => { status.textContent = ""; }, 4000);
  };

  // the right part of the row: recording strip, the picker on the left, the action icon. Rebuilt when the mode changes, not on every snapshot
  let strip = null, action = null, leading = null, modeSig = null, leadSig = null;
  // while recording, the row holds only the strip and the icon: the picker on the left does not crowd it
  const barParts = () => [...(strip ? [strip] : []), ...(leading && !strip ? [leading] : []), hint, spacer, ...(action.hubDict ? [action.hubDict] : []), action];

  function draw() {
    const list = (S.pending[key] = S.pending[key] || []);
    // the paperclip icon
    if (attachIcon !== kitSig("attach")) { attachIcon = kitSig("attach"); attach.replaceChildren(icon("attach")); }
    if (closeIcon !== kitSig("close")) { closeIcon = kitSig("close"); replyCancel.replaceChildren(icon("close")); }
    // the recipient, the reply-to
    form.classList.toggle("hk", !!o.hotkey);
    setText(toName, sessionName(o.to || ""));
    toClear.hidden = !o.reply;
    if (clearIcon !== kitSig("close")) { clearIcon = kitSig("close"); toClear.replaceChildren(icon("close")); }
    const clear = t("web.composer.to_clear", { name: sessionName(o.to || "") });
    setAttr(toClear, "aria-label", clear);
    setAttr(toClear, "title", clear);
    const r = o.reply, rSig = r ? [r.title || "", r.quote || "", r.text || ""].join("\0") : "";
    if (replyChip.__sig !== rSig) {
      replyChip.__sig = rSig;
      replyBar.classList.toggle("quote", !!r?.quote);
      replyChip.replaceChildren(...(!r ? [] : r.quote ? [...(r.title ? [el("span", "reply-title", r.title)] : []), el("span", "reply-quote", r.quote)]
        : [document.createTextNode(r.text || "")]));
    }
    // attachment chips: rebuilt when the list or the icon changes
    const sig = list.map((p) => p.key + (p.meta ? "+" : "")).join(",") + "|" + kitSig("attach") + kitSig("close");
    if (sig !== chipsSig) { chipsSig = sig; drawChips(list); }
    // the field itself
    const draft = S.drafts[key] || "";
    if (ta.value !== draft) ta.value = draft;
    if (o.commands && ta.value !== menuFor) drawMenu();   // the field was cleared by sending or text was substituted — the window follows
    // the core's own placeholder is written when it (or the look) changes, not when the live one differs: a theme may put
    // a shorter one there (Clean), and the two rewrote each other on every snapshot
    const ph = o.placeholder || "", phSig = lookSig() + "\0" + ph;
    if (ta.__ph !== phSig) { ta.__ph = phSig; ta.placeholder = ph; }
    if (label) setText(label, o.label || "");
    else setAttr(ta, "aria-label", o.label || o.placeholder || t("web.composer.message_to", { to: sessionName(o.to) }));
    setText(hint, said || o.hint || "");
    setText(note, answer ? said : "");
    note.hidden = !answer;
    setText(err, S.errors[key] || "");
    if (linkHeld.has(key)) {
      const go = el("button", "btn small primary", t("web.bus.composer_link_go"));
      const plain = el("button", "btn small quiet", t("web.bus.composer_link_text"));
      go.type = plain.type = "button";
      go.addEventListener("click", () => { linkHeld.delete(key); const text = ta.value; S.drafts[key] = ta.value = ""; draw(); pasteIntoHubs(text); });
      plain.addEventListener("click", () => { linkHeld.delete(key); asText.set(key, ta.value); submit(); });
      err.replaceChildren(el("span", null, t("web.bus.composer_link")), go, plain);
    }
    const lost = S.errors[key] && failedRecording(key);   // the recording was not transcribed — do not discard it
    if (lost) {
      err.append(...keptLinks({
        // another recording is running — nothing. "Retry" is a new "send": addressed with what the field has now
        retry: () => { if (!recording()) transcribeRecording(lost, (!lost.insert && lock()) || lost.sendText, lost.fillText); },
        download: () => retryDownload(lost),
      }));
    }
    const redo = S.errors[key] && failedDictation(key);   // the same for a dictation into the field
    if (redo) err.append(...keptLinks(redo));
    // recording strip, action icon, picker on the left
    const fresh = recStrip(key, fillText);   // while recording: instead of the field — a dot, the time, "Done", "Cancel"
    const mode = [voiceMode(), S.snap?.voice ? "voice" : "", list.map((p) => p.key + (p.meta ? "+" : "")).join(","), o.to, fresh ? "rec" : ""].join("|");
    if (mode !== modeSig) {
      modeSig = mode;
      strip = fresh;
      action = actionButton({ name: key }, ta, (text) => submit(text), fillText, lock);
    }
    action.hubPaint?.();   // the text may have been set by code: no input event, so the button repaints here
    form.classList.toggle("rec", !!strip);
    const lead = o.leading ? o.leadingSig || "" : null;
    if (lead !== leadSig) { leadSig = lead; leading = o.leading ? o.leading() : null; }
    form.classList.toggle("has-leading", !!leading);   // a picker on the left keeps its own layout (--composer-wrap)
    sync(bar, barParts());
    layout();
    grow();
  }

  // the words of a recording ended with "Done": into the draft after what is typed, not sent. The draft is the one source: the
  // field may have been rebuilt meanwhile (a session switch and back)
  function fillText(text) {
    const rest = (S.drafts[key] || "").replace(/\s+$/, "");
    S.drafts[key] = ta.value = rest ? `${rest} ${text}` : text;
    if (!ta.isConnected) return boxes.get(key)?.refresh();
    draw();
    grow(true);
  }

  function drawChips() { chips.replaceChildren(...chipNodes(key, draw)); }

  const session = () => (S.snap?.sessions || []).find((x) => x.name === o.commands);
  function drawMenu() {
    menuFor = ta.value;
    found = o.commands ? suggest(ta.value) : [];
    pick = Math.min(pick, Math.max(found.length - 1, 0));
    ta.setAttribute("aria-expanded", String(!!found.length));
    if (!found.length) { ta.removeAttribute("aria-activedescendant"); menu.replaceChildren(); return layout(); }
    const rows = el("div", "cmd-rows");
    rows.append(...found.map((c, i) => {
      const row = el("div", "cmd-row");
      row.id = `${menu.id}-${i}`;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(i === pick));
      const nm = el("span", "nm", "/" + c.name);
      if (c.args) nm.append(el("span", "args", " " + c.args));
      row.append(nm, el("span", "what", c.hint || ""), el("span", "by", c.by));
      row.addEventListener("mouseenter", () => { pick = i; mark(); });
      row.addEventListener("click", () => choose(c, true));
      return row;
    }));
    menu.replaceChildren(rows, el("div", "cmd-foot", t("web.composer.commands_foot")));
    mark();
    layout();
  }
  function mark() {
    [...menu.querySelectorAll(".cmd-row")].forEach((r, i) => r.setAttribute("aria-selected", String(i === pick)));
    ta.setAttribute("aria-activedescendant", `${menu.id}-${pick}`);
    menu.querySelector(`[aria-selected="true"]`)?.scrollIntoView({ block: "nearest" });
  }
  // a row was chosen: if the command needs arguments, complete the name and wait for them; otherwise run it at once
  function choose(c, go) {
    ta.value = S.drafts[key] = `/${c.name}` + (c.args ? " " : "");
    if (go && !(c.args || "").startsWith("<")) return submit();
    drawMenu();
    grow();
    ta.focus();
  }
  // a core or plugin command — without a session; the result goes into the hint under the field, an error goes under the field, the typed text comes back
  async function command(r, words) {
    S.drafts[key] = ta.value = "";
    S.errors[key] = "";
    drawMenu();
    draw();
    try {
      const done = await runCommand(r, session());
      setFlash(done, true);
      // a command result stays longer than "Sent …", and the longer the more lines it has (the deputies of /away); an
      // equal answer to the next command is another one: only its own timer clears it
      const no = answerNo;
      setTimeout(() => { if (answerNo === no) setFlash(""); }, 10000 + 5000 * (done.split("\n").length - 1));
    } catch (e) {
      if (!ta.value) ta.value = S.drafts[key] = words;
      S.errors[key] = e.message || String(e);
      draw();
    }
    ta.focus();
  }

  // A voice message is addressed when "send" is pressed (the button, Space, the release of a hold, the silence stop), not
  // when its words are ready: the reply target, the board cards and the way it goes (a question card's answer or a plain
  // message) of that moment go with it, and so do the text and the files then in the field. The reply quote, the cards and
  // a card's aim leave the field at once, as after a typed send; what the owner sets, types or adds while it is
  // transcribed belongs to his next message.
  // Returns send(text, client_id) for those words, with .session (whose feed shows it meanwhile; none for a card's answer)
  // and .drop() (nothing was sent: cancelled, no speech, the transcription failed — the reply, the cards and the aim come
  // back unless he has pointed the field elsewhere since). A field without aim() (a plugin's) sends the old way: null.
  // So does a field whose aim, body or before throws: the recording still goes, addressed when it is transcribed — else
  // the voice message was lost and the field stayed on "recognizing" with nothing behind it
  function lock() {
    try { return locked(); } catch (err) { console.error(err); return null; }
  }
  function locked() {
    if (!o.aim) return null;
    const at = { aim: o.aim(), body: o.body, send: o.send, holdLinks: o.holdLinks, failed: o.failed, restore: o.restore,
      files: new Set(S.pending[key] || []), draft: S.drafts[key] || "" };
    const probe = at.body("", [], at.aim);
    o.before?.(probe, true);
    const drop = () => { at.restore?.(probe); at.failed?.(probe); draw(); };
    const send = (text, cid) => submit(text, { ...at, cid, drop });
    send.session = at.send ? null : probe.session;
    send.drop = drop;
    return send;
  }

  // held — a voice message addressed when "send" was pressed (lock): its reply, cards and transport, and the text and the
  // files of that moment. What was typed or added later stays in the field (a file with its mark); held.drop() wherever
  // it stops short of sending gives the field back what the press took
  async function submit(override, held = null) {
    // the words come from outside (a voice message): into the draft too, or any refusal below redraws the field from the old draft.
    // They go in front of the draft: it keeps the "[Image #N]" marks of screenshots added during the recording and the words
    // typed after a failed transcription. A file of the press still uploading rides along: the words wait for it
    let lead = null;   // what goes: the words and the field's text of the press (all of it, if edited since)
    if (override != null) {
      const typed = S.drafts[key] || "";
      const own = held && typed.startsWith(held.draft) ? held.draft : typed;
      const tail = typed.slice(own.length);
      lead = own.trim() ? `${override} ${own.trim()}` : override;
      S.drafts[key] = ta.value = lead + (tail && !/^\s/.test(tail) ? " " : "") + tail;
      const waited = (S.pending[key] || []).filter((p) => !p.meta && (!held || held.files.has(p)));
      if (waited.length) {   // the words wait in the field, chips say "uploading"
        draw();
        await uploaded(key, held && waited);
        // the draft is the one source: the field may have been rebuilt meanwhile (a session switch and back) and edited there
        if (!ta.isConnected) ta.value = S.drafts[key] || "";
        // the person sent them by hand meanwhile (with the field's aim of that moment); an upload failed: the words stay
        // in the field next to its error. Either way the reply and the cards of the press come back
        if (!ta.value.trim() && !(S.pending[key] || []).length) return held?.drop();
        if (waited.some((p) => p.failed)) return held ? held.drop() : draw();
      }
      // a file of the press failed to upload while the words were recognized (it is out of the field's files by now):
      // the words stay in the field next to its error, the reply and the cards come back — never sent without it
      const bad = held && [...held.files].find((p) => p.failed);
      if (bad) { S.errors[key] = bad.failed; return held.drop(); }   // a successful transcription cleared the field's line
    }
    const way = held || o;   // the transport and the aim: of the press for a voice message, of this moment for a typed one
    const all = S.pending[key] || [];
    const later = held ? all.filter((p) => !held.files.has(p)) : [];
    const files = held ? all.filter((p) => held.files.has(p)) : all;
    const goes = held && ta.value.startsWith(lead) ? lead : ta.value;
    const left = ta.value.slice(goes.length).trim();   // a voice message: typed after its "send"
    const words = later.reduce(withoutMark, goes).trim();
    const r = o.commands && !held ? resolve(words) : null;
    if (r && r.kind !== "none") return command(r, words);   // not a listed command (a skill, someone else's "/name") — goes to the session as plain text
    // a plugin transport (send) takes links as is; a session field with its own send (a question card answer) holds them
    if ((!way.send || way.holdLinks) && busLinkIn(words) && asText.get(key) !== ta.value) { linkHeld.add(key); return held ? held.drop() : draw(); }
    asText.delete(key);
    if (files.some((p) => !p.meta)) { S.errors[key] = t("web.composer.wait_upload"); return held ? held.drop() : draw(); }
    if (!words && !files.length) { S.errors[key] = t("web.composer.empty"); return held ? held.drop() : draw(); }
    if (said === t("web.composer.send_stuck")) setFlash("");
    const body = held ? held.body(words, files, held.aim) : o.body(words, files);
    const was = held ? null : unsent.get(key);
    body.client_id = held?.cid || (was && was.sig === sendSig(body) ? was.client_id : newClientId());
    claim(body.client_id, body.text);   // the feed's bubble of a voice message (outgoing.js) now waits for this very send
    // a typed message to a session is in its feed at once, "sending", until the core's row takes its place (outgoing.js:
    // only where that row will say the same words); a plugin's own transport (a card answer, another hub) has no feed row
    if (!held && !way.send) typed(body.client_id, body, files);
    // A late receipt owns only the retry record seen when this send began.
    // Another in-flight send may since have restored its own uncertain outcome.
    const clearUnsent = () => { if (!held && unsent.get(key) === was) unsent.delete(key); };
    // the field and attachments are cleared at once: a second quick message will not carry them again; on error — restore them.
    // A voice message leaves what was added after its "send" (the files and their marks); its reply and cards left the field then
    S.drafts[key] = [left, ...later.filter((p) => p.label && !left.includes(`[${p.label}]`)).map((p) => `[${p.label}]`)].filter(Boolean).join(" ");
    ta.value = S.drafts[key];
    S.pending[key] = later;
    S.errors[key] = "";
    if (!held) o.before?.(body);
    draw();
    if (!ta.isConnected) boxes.get(key)?.refresh();   // a voice message that waited for its files: the rebuilt field empties too
    let res;
    try { res = (await (way.send ? way.send({ ...body }) : sendMessage(body))) || {}; }
    catch { res = { receipt: "unknown", error: t("web.api.no_answer") }; }
    if (res.error || res.ok === false) {
      const error = sentence(String(res.error || t("web.api.rejected")));
      // refusal — the same text will go again with a new id; unknown outcome — a repeat of the same with the old id
      if (res.receipt === "refused") clearUnsent();
      else unsent.set(key, { sig: sendSig(body), client_id: res.client_id || body.client_id });
      // a plugin transport's own phrase is shown as is; the core's phrase says that the text came back into the field
      settled(body.client_id);
      giveBack(words, files, way.send ? error
        : t(res.receipt === "unknown" ? "web.composer.send_unknown" : "web.composer.send_failed", { error }), false, body, way);
    } else {
      clearUnsent();
      // the bubble leaves when the core's row with this seq is in the feed; still queued — follow() says when
      if (!(res.receipt === "queued" && !way.send)) sent(body.client_id, res.data?.seq);
      // the core did not decide within 3 s (API 1.29) — ask for the outcome later; a plugin's own transport handles it itself
      if (res.receipt === "queued" && !way.send && body.client_id) follow(body, words, files, way);
      // while the field waits for a receipt, the caller's "Sent…" does not override the waiting caption
      o.sent?.({ text: body.text, to: o.to }, (text) => { if (!waits) setFlash(text); });
    }
    draw();
    ta.focus();
  }

  // text and files go back into the field (before whatever was typed since), with the reason under the field. late —
  // the outcome came later (a receipt): the caller's failed() would restore the reply target of an already later send.
  // way — whose restore/failed: a voice message's are those of its "send"
  function giveBack(words, files, why, late, body, way = o) {
    S.drafts[key] = words + (S.drafts[key] ? "\n" + S.drafts[key] : "");
    S.pending[key] = files.concat(S.pending[key] || []);
    S.errors[key] = why;
    ta.value = S.drafts[key];
    way.restore?.(body);   // the cards of this very send (a late refusal too: they belong to the message, not to the reply target)
    if (!late) way.failed?.(body);
    draw();
    grow();
    // the field may have been rebuilt (another session and back; a voice message that waited for its files) — the visible
    // one takes the draft and error from S
    if (late || !ta.isConnected) render();
  }

  // Receipt after queued: ask less and less often until the core decides. Refusal — the text goes back into the field
  // with the core's reason. If the core never accepted it and it is not in the outbox (unknown) for a full minute — it
  // goes back too: a retry yields a single message. 10 minutes without a decision — say so and stop asking: do not
  // return the text, the core will deliver it when it frees up (otherwise a duplicate).
  // The waiting caption is one per field while any send is waiting (waits)
  async function follow(body, words, files, way) {
    const wait = t("web.composer.send_queued");
    waits++;
    setFlash(wait);
    let stuck = false;
    try {
      const got = await awaitDecision(body.session, body.client_id);
      if (got.receipt === "accepted") sent(body.client_id, got.seq); else settled(body.client_id);
      if (got.receipt === "refused")
        return giveBack(words, files, t("web.composer.send_failed", { error: sentence(String(got.error || t("web.api.rejected"))) }), true, body, way);
      if (got.receipt === "lost") {
        // the same without edits — with the same id: if the first one still reaches the core, there will be no second message
        unsent.set(key, { sig: sendSig(body), client_id: body.client_id });
        return giveBack(words, files, t("web.composer.send_lost"), true, body, way);
      }
      if (got.receipt === "stuck") stuck = true;   // in the outbox or no connection — 10 minutes
    } finally {
      waits--;
      stuckAny = stuckAny || stuck;
      // the caption — when none is waiting: "still in the outbox" if at least one ended that way (the next send clears it)
      if (!waits && said === wait) setFlash(stuckAny ? t("web.composer.send_stuck") : "");
      if (!waits) stuckAny = false;
    }
  }

  ta.addEventListener("input", () => {
    S.drafts[key] = ta.value; grow(); drawMenu();
    if (linkHeld.delete(key)) draw();   // the text changed: the question about the link goes with it
  });
  ta.addEventListener("keydown", (e) => {
    if (found.length && !e.isComposing) {
      const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
      if (step) { e.preventDefault(); pick = (pick + step + found.length) % found.length; return mark(); }
      if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); return choose(found[pick], false); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); found = []; menu.replaceChildren(); return layout(); }
      // the full name is not typed yet ("/", "/co") — Enter only completes the selection: the command runs on a second
      // Enter, so a single accidental Enter does not compact the conversation or restart the session
      if (e.key === "Enter" && !e.shiftKey && ta.value.trim() !== `/${found[pick].name}`) { e.preventDefault(); return choose(found[pick], false); }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  ta.addEventListener("paste", (e) => {
    const files = o.files === false ? [] : droppedFiles(e.clipboardData);
    if (files.length) { e.preventDefault(); addFiles(key, files); return; }
    const text = o.files === false ? "" : e.clipboardData?.getData("text/plain") || "";
    if (longPaste(text)) { e.preventDefault(); pasteTile(key, text); }
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  // the field has its own highlight; the general one (the whole feed) is cleared so it neither doubles nor sticks after a drop
  const offMainDrop = () => document.getElementById("main")?.classList.remove("dragging");
  form.addEventListener("dragover", (e) => {
    if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) {   // without files too: otherwise the browser opens the dropped file in the tab
      e.preventDefault(); e.stopPropagation(); offMainDrop();
      if (o.files !== false) form.classList.add("dragging");
    }
  });
  form.addEventListener("dragleave", (e) => { if (!form.contains(e.relatedTarget)) form.classList.remove("dragging"); });
  form.addEventListener("drop", (e) => {
    form.classList.remove("dragging");
    offMainDrop();
    if (!e.dataTransfer?.files.length) return;
    e.preventDefault();
    e.stopPropagation();
    if (o.files !== false) addFiles(key, droppedFiles(e.dataTransfer));
  });
  // a click anywhere in the block puts the cursor in the field, like a chat (the whole block counts: hint, padding)
  form.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target === ta || e.target.closest("button, a, input, select, label, .pchip, .recstrip")) return;
    e.preventDefault();   // do not lose focus and do not select the hint
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  });

  boxes.set(key, { ta, refresh: draw, flash, files: o.files !== false });   // attachments and voice find the field by draft key
  draw();
  requestAnimationFrame(() => grow());   // a plugin's node is put on the page only after the first render
  // a new width rewraps the text (the window, the side panel); a field shown after being hidden gets its height back
  if (typeof ResizeObserver === "function") {
    let width = -1;
    new ResizeObserver((all) => {
      const w = all[all.length - 1].contentRect.width;
      if (w !== width) { width = w; grow(true); }
    }).observe(ta);
  }
  return {
    node: form,
    update(next) { if (next) Object.assign(o, next); draw(); },
    refresh: draw,
    focus: () => ta.focus(),
    // start a voice recording as the microphone button does (call it from a click: iOS gives the microphone only to a
    // gesture); true — it started opening, false — voice is off or unavailable here, something is recording already, or the field
    // holds a draft (the recognized words would replace it)
    record: () => (ta.isConnected && !ta.value.trim() && !(S.pending[key] || []).length && action?.hubRecord ? action.hubRecord() : false),
    ta: () => ta,
    // field draft (API 1.28): the text, uploaded attachments (/api/upload metadata) and how many are still uploading
    draft: () => ({ text: S.drafts[key] || "", files: (S.pending[key] || []).filter((p) => p.meta).map((p) => ({ ...p.meta })),
                    uploading: (S.pending[key] || []).filter((p) => !p.meta).length }),
    // restore or replace the draft: files — metadata from draft() or /api/upload
    setDraft(next = {}) {
      if (typeof next.text === "string") S.drafts[key] = next.text;
      if (Array.isArray(next.files)) {
        S.pending[key] = next.files.filter((m) => m && typeof m.path === "string").map((m, i) => ({
          key: `d${Date.now()}-${i}`, name: m.name || m.path.split("/").pop(), size: m.size || 0, meta: { ...m },
          preview: null, label: m.label || null }));
      }
      draw();
    },
  };
}
