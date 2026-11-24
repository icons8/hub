// Messenger keys of the open conversation, as in Telegram Desktop and Slack (the same in the native clients):
// - ⌘↑ / ⌘↓ (Ctrl+↑ / Ctrl+↓ off a Mac) in an empty field picks a feed row to reply to: up — older, down — newer, down
//   past the newest drops the reply. Only in the field and only with it empty: with words in it the keys stay the system's
//   (caret to the start or the end), with the focus on the feed they scroll it as before (as Telegram and Slack). Rows whose reply would go to another session are skipped. Ctrl+↑ on a Mac is the system's (Mission
//   Control), ⌘⌥ and ⇧ arrows are left alone;
// - Esc with a reply quote over the field drops the quote — it no longer goes on to stop the session's turn or close a
//   question; Esc in another field (the session search) is that field's own, even when it moves the focus here; an image on top of the feed closes first (lightbox.js), a recording keeps its own Esc (recorder.js);
// - a letter typed while the focus is outside any field goes into the conversation's field. Space is not taken: outside
//   fields it records a voice message (recorder.js).
// The listeners are registered before main.js's own keys: Esc taken here is marked handled, main.js skips it.
import { $ } from "./dom.js";
import { S, render } from "./state.js";
import { replyTo } from "./feed.js";

const MAC = /mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform || "");
const EDITABLE = "input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='menu'], [role='menuitem'], [role='listbox'], [role='option']";

const field = () => { const ta = $("msg"); return ta && ta.isConnected && ta.offsetParent !== null ? ta : null; };
const quoteHere = () => !!S.cur && S.replyTarget?.view === S.cur.name;
const overlay = () => !!document.querySelector("dialog[open], [aria-modal='true']:not([hidden])");   // a dialog, the image on top (lightbox.js)

// the rows a reply can go from, oldest first: the "Reply" buttons of the open feed that reply in this conversation and
// are on screen (a folded group's rows are not)
function candidates() {
  return [...document.querySelectorAll("main .feed .reply-btn")]
    .filter((b) => b.__reply && b.__reply.session === S.cur.name && b.offsetParent !== null);
}

// the row picked from the keyboard: the next press goes on from it; a quote picked with "Reply" or a row redrawn since —
// by the quote, the nearest from the end
let picked = null;   // {target — S.replyTarget it set, btn}
function at(all) {
  if (!quoteHere()) return -1;
  if (picked?.target === S.replyTarget) {
    const i = all.indexOf(picked.btn);
    if (i >= 0) return i;
    // the row was redrawn: the row with the same words at the same place from the end (two "Done." do not loop)
    const j = all.length - 1 - picked.fromEnd;
    if (all[j]?.__reply.quote === S.replyTarget.quote) return j;
  }
  return all.findLastIndex((b) => b.__reply.quote === S.replyTarget.quote);
}

function step(dir) {
  const all = candidates();
  if (!all.length) return false;
  const now = at(all);
  if (dir > 0 && now < 0) return false;   // ↓ without a picked row is the system's
  const next = now < 0 ? all.length - 1 : now + dir;
  if (next >= all.length) { S.replyTarget = null; render(); return true; }   // down past the newest — no reply
  const b = all[Math.max(0, next)];
  replyTo(b.__reply);
  picked = { target: S.replyTarget, btn: b, fromEnd: all.length - 1 - all.indexOf(b) };
  // the render of the quote may rebuild the row (one with a picture): scroll to the row as drawn now
  const now2 = candidates(), i = at(now2);
  if (i >= 0) picked.btn = now2[i];
  picked.btn.closest(".row")?.scrollIntoView({ block: "nearest" });
  return true;
}

document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || e.isComposing || e.keyCode === 229 || !S.cur || overlay()) return;
  const ta = field();
  if (!ta) return;
  const here = document.activeElement === ta || !document.activeElement?.closest?.(EDITABLE);
  // reply from the keyboard
  if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !e.altKey && !e.shiftKey && (MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey)) {
    if (document.activeElement !== ta || ta.value !== "") return;
    if (step(e.key === "ArrowUp" ? -1 : 1)) e.preventDefault();
    return;
  }
  const other = e.target !== ta && !!e.target?.closest?.(EDITABLE);   // the key began in another field
  if (e.key === "Escape" && !e.metaKey && !e.ctrlKey && !e.altKey && quoteHere() && here && !other) {
    e.preventDefault();
    S.replyTarget = null;
    render();
    return;
  }
  // typing outside the field: the letter goes into it (the default action lands in the newly focused field)
  if (e.metaKey || e.ctrlKey || e.altKey || e.key === " " || [...e.key].length !== 1 || document.activeElement === ta || !here) return;
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
});
