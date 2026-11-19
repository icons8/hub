// File comments, as in Google Docs: select a piece, click "Comment", a thread opens on the right.
// A comment is an ordinary message to the session (the same chat); the session's turn result appears as a reply in the thread; a question
// gets an answer, and if a rewrite is wanted the session asks in the reply (no "ask to rewrite" button: let it ask in the comments).
// Threads are stored by "Files" (modules/files/server.py), pieces are highlighted (CSS Custom Highlight: the page is not rebuilt).
import { $, el } from "/web/js/dom.js";
import { md } from "/web/js/markdown.js";
import { t } from "/web/js/i18n.js";

const C = { root: null, rel: null, abs: null, threads: [], session: null, sessions: [], content: null, pending: null,
  drafts: {} };
const HL = typeof CSS !== "undefined" && CSS.highlights && typeof Highlight !== "undefined";
const q = (root, rel) => `root=${encodeURIComponent(root)}&path=${encodeURIComponent(rel)}`;

async function get(url) { try { const r = await fetch(url); return r.ok ? r.json() : null; } catch { return null; } }
async function post(url, body) {
  try {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Hub": "1" },
      body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    return r.ok ? { data: d } : { error: d.error || t("files.not_sent") };
  } catch { return { error: t("files.no_answer") }; }
}

// file opened (or re-read): content is the element with the file text; null means nothing to comment on (image, edit)
export async function attach({ root, rel, abs, content, session }) {
  const same = root === C.root && rel === C.rel;
  C.content = content;
  if (!same) {
    Object.assign(C, { root, rel, abs, threads: [], session: session || null, sessions: [] });
    get(`/api/sessions?path=${encodeURIComponent(abs)}`).then((ss) => {
      C.sessions = Array.isArray(ss) ? ss : [];
      if (!C.sessions.some((s) => s.name === C.session))
        C.session = (C.sessions.find((s) => s.here) || C.sessions.find((s) => s.hub) || {}).name || null;
    });
  }
  hideButton();
  await refresh(true);
}

export function detach() { C.content = null; hideButton(); panel(); paint(); }

// new session replies: every few seconds while the file is open
export async function refresh(force) {
  if (!C.root) return;
  const t = await get(`/api/comments?${q(C.root, C.rel)}`);
  if (!Array.isArray(t)) return;
  const sig = (xs) => JSON.stringify(xs.map((x) => [x.id, x.messages.length, x.resolved]));
  const changed = sig(C.threads) !== sig(t);
  C.threads = t;
  if (changed || force) {
    const typing = document.activeElement?.closest?.("#cpanel textarea");  // do not interrupt typing a reply
    if (!typing || force) panel();
    paint();
  }
}

// --- selection -> button -> comment ------------------------------------------------------------------------
function selectionIn() {
  const sel = getSelection();
  if (!C.content || !sel.rangeCount || sel.isCollapsed) return null;
  const r = sel.getRangeAt(0);
  if (!C.content.contains(r.commonAncestorContainer)) return null;
  const quote = sel.toString().trim();
  return quote.length >= 2 ? { range: r.cloneRange(), quote } : null;
}

function showButton() {
  const s = selectionIn(), b = $("cbtn");
  if (!s) return hideButton();
  const rect = s.range.getBoundingClientRect();
  b.hidden = false;
  if (innerWidth > 760) {
    b.style.top = `${Math.min(innerHeight - 44, Math.max(8, rect.bottom + 6))}px`;
    b.style.left = `${Math.min(innerWidth - 190, Math.max(8, rect.right - 80))}px`;
    b.classList.remove("bottom");
  } else b.classList.add("bottom");  // on a phone it goes at the bottom of the screen: a selection has its own system menu
}
function hideButton() { const b = $("cbtn"); if (b) b.hidden = true; }

function lineOf(range) {
  const n = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
  const ln = n && n.closest(".ln");
  if (!ln || !C.content.contains(ln)) return null;
  return [...C.content.querySelectorAll(".ln")].indexOf(ln) + 1 || null;
}

function sessionSelect() {
  const who = el("select");
  who.setAttribute("aria-label", t("files.to_whom"));
  for (const s of C.sessions) {
    const o = el("option", null, s.name + (s.here ? t("files.session_here") : "") + (s.state === "asleep" ? t("files.session_asleep") : ""));
    o.value = s.name;
    o.selected = s.name === C.session;
    who.append(o);
  }
  if (!C.sessions.length && C.session) { const o = el("option", null, C.session); o.value = C.session; who.append(o); }
  who.addEventListener("change", () => { C.session = who.value; });
  return who;
}

function compose() {
  const s = selectionIn();
  if (!s) return;
  hideButton();
  C.pending = { quote: s.quote, line: lineOf(s.range), nth: nthOf(s.range, s.quote) };
  const box = $("ccompose");
  const ta = el("textarea");
  ta.placeholder = t("files.compose_placeholder");
  ta.rows = 3;
  const go = el("button", "btn primary", t("files.comment_btn")), cancel = el("button", "btn", t("files.cancel"));
  go.type = cancel.type = "button";
  const who = sessionSelect();
  const close = () => { C.pending = null; box.hidden = true; };
  const sendIt = async () => {
    if (!ta.value.trim()) return;
    go.disabled = true;
    const r = await post("/api/comment", { session: who.value || C.session, root: C.root, path: C.rel,
      text: ta.value.trim(), ...C.pending });
    go.disabled = false;
    if (r.error) { notify(r.error, true); return; }
    getSelection().removeAllRanges();
    close();
    notify(t("files.sent_to", { name: r.data.session }));
    await refresh(true);
  };
  go.addEventListener("click", sendIt);
  cancel.addEventListener("click", close);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendIt(); }
    if (e.key === "Escape") close();
  });
  const quote = C.pending.quote, row = el("div", "crow");
  row.append(go, cancel);
  box.replaceChildren(el("div", "cq", quote.length > 240 ? quote.slice(0, 240) + "…" : quote), ta, who, row,
    el("div", "hint", t("files.send_hint")));
  box.hidden = false;
  ta.focus();
}

// --- finding a piece in the text: ignoring spaces and line breaks, code line numbers do not count --------------------------
function chars(root) {
  const out = [];  // [node, offset, char]: non-whitespace only
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement?.closest(".mk") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT) });
  while (walk.nextNode()) {
    const t = walk.currentNode, s = t.nodeValue;
    for (let i = 0; i < s.length; i++) if (!/\s/.test(s[i])) out.push([t, i, s[i]]);
  }
  return out;
}
const squash = (s) => s.replace(/\s+/g, "");

function nthOf(range, quote) {
  const cs = chars(C.content), text = cs.map((c) => c[2]).join(""), sq = squash(quote);
  const start = cs.findIndex(([n, i]) => range.comparePoint(n, i) >= 0);
  let nth = 0;
  for (let at = text.indexOf(sq); at >= 0 && at < start; at = text.indexOf(sq, at + 1)) nth++;
  return nth;
}

function rangeOf(t, cs, text) {
  const sq = squash(t.quote || "");
  if (!sq) return null;
  let at = text.indexOf(sq);
  for (let k = 0; k < (t.nth || 0) && at >= 0; k++) at = text.indexOf(sq, at + 1);
  if (at < 0 && (t.nth || 0) > 0) at = text.indexOf(sq);  // file was rewritten: take the first occurrence
  if (at < 0) return null;
  const r = document.createRange(), a = cs[at], b = cs[at + sq.length - 1];
  r.setStart(a[0], a[1]);
  r.setEnd(b[0], b[1] + 1);
  return r;
}

const ranges = {};
function paint() {
  if (!HL) return;
  CSS.highlights.delete("fcom");
  CSS.highlights.delete("fcom-on");
  if (!C.content) return;
  const cs = chars(C.content), text = cs.map((c) => c[2]).join(""), rs = [];
  for (const t of C.threads) {
    ranges[t.id] = rangeOf(t, cs, text);
    if (ranges[t.id] && !t.resolved) rs.push(ranges[t.id]);
  }
  if (rs.length) CSS.highlights.set("fcom", new Highlight(...rs));
  for (const b of document.querySelectorAll("#cpanel .cq[data-id]")) b.classList.toggle("gone", !ranges[b.dataset.id]);
}

// --- right panel: threads ----------------------------------------------------------------------------------------
function panel() {
  const p = $("cpanel");
  if (!p) return;
  const on = !!C.content && C.threads.length > 0;
  $("app").classList.toggle("commenting", on);
  p.hidden = !on;
  if (!on) { p.replaceChildren(); return; }
  const live = C.threads.filter((t) => !t.resolved), done = C.threads.filter((t) => t.resolved);
  const list = el("div", "clist");
  list.append(...live.map(thread));
  const kids = [el("h3", null, t("files.comments_title", { count: live.length })), list];
  if (done.length) {
    const d = el("details", "cdone");
    d.append(el("summary", null, t("files.closed_title", { count: done.length })), ...done.map(thread));
    kids.push(d);
  }
  p.replaceChildren(...kids);
}

function thread(th) {
  const box = el("div", "citem" + (th.resolved ? " resolved" : ""));
  const quote = el("button", "cq", th.quote.length > 140 ? th.quote.slice(0, 140) + "…" : th.quote);
  quote.type = "button";
  quote.dataset.id = th.id;
  quote.title = t("files.show_in_file");
  quote.addEventListener("click", () => focus(th));
  box.append(quote);
  for (const m of th.messages) {
    const own = m.who === "owner";
    const msg = el("div", "cmsg" + (own ? " own" : ""));
    msg.append(el("div", "cwho", own ? t("files.you") : m.who), own ? el("div", "ct", m.text) : md(m.text, { breaks: true }));
    box.append(msg);
  }
  const last = th.messages[th.messages.length - 1];
  if (last && last.who === "owner" && !th.resolved) box.append(el("div", "cwait", t("files.awaiting", { name: th.session })));
  const row = el("div", "crow");
  if (!th.resolved) {
    const ta = el("textarea");
    ta.rows = 1;
    ta.placeholder = t("files.reply");
    ta.value = C.drafts[th.id] || "";
    ta.addEventListener("input", () => { C.drafts[th.id] = ta.value; });
    const reply = el("button", "btn", t("files.reply")), close = el("button", "btn quiet", t("files.close"));
    reply.type = close.type = "button";
    const sendIt = async () => {
      if (!ta.value.trim()) return;
      const r = await post("/api/comment", { thread: th.id, root: C.root, path: C.rel, text: ta.value.trim() });
      if (r.error) { notify(r.error, true); return; }
      delete C.drafts[th.id];
      ta.blur();
      await refresh(true);
    };
    reply.addEventListener("click", sendIt);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendIt(); } });
    close.addEventListener("click", async () => {
      const r = await post("/api/comment/resolve", { thread: th.id, root: C.root, path: C.rel });
      if (r.error) notify(r.error, true); else await refresh(true);
    });
    row.append(reply, close);
    box.append(ta, row);
  } else {
    const again = el("button", "btn quiet", t("files.reopen"));
    again.type = "button";
    again.addEventListener("click", async () => {
      await post("/api/comment/resolve", { thread: th.id, root: C.root, path: C.rel, reopen: true });
      await refresh(true);
    });
    row.append(again);
    box.append(row);
  }
  return box;
}

function focus(th) {
  const r = ranges[th.id];
  if (!r) { notify(t("files.passage_gone"), true); return; }
  const rect = r.getBoundingClientRect(), v = $("view");
  v.scrollTo({ top: v.scrollTop + rect.top - v.getBoundingClientRect().top - 80, behavior: "smooth" });
  if (HL) { CSS.highlights.set("fcom-on", new Highlight(r)); setTimeout(() => CSS.highlights.delete("fcom-on"), 1600); }
}

let notify = () => {};
export function init(banner) {
  notify = banner;
  const b = el("button", "btn primary cbtn", `💬 ${t("files.comment_btn")}`);
  b.id = "cbtn";
  b.type = "button";
  b.hidden = true;
  b.addEventListener("mousedown", (e) => e.preventDefault());  // keep the selection
  b.addEventListener("click", compose);
  const box = el("div", "ccompose");
  box.id = "ccompose";
  box.hidden = true;
  document.body.append(b, box);
  let wait;
  document.addEventListener("selectionchange", () => { clearTimeout(wait); wait = setTimeout(showButton, 150); });
  $("view").addEventListener("scroll", hideButton);
  setInterval(() => { if (!document.hidden && C.content) refresh(); }, 4000);
}
