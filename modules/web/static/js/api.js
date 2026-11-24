// Connection to the page's server: the live core snapshot (SSE) and actions (POST with the X-Hub header).
import { $, icon, kit } from "./dom.js";
import { S, renderLive } from "./state.js";
import { staleVersion } from "./updates.js";
import { adopt, retryFailed, sync } from "./timeline.js";
import { t, bare } from "./i18n.js";
import { recording } from "./voice.js";
import { failedOnce, showWait } from "./backoff.js";
import { applyPatch } from "./stream-delta.js";
import { shareTextScale } from "./textscale.js";

let bannerTimer, bannerStays = false;
// another banner faded or was closed while there is no connection: the connection status comes back at once, not with
// the next attempt (a pause is up to 60 s)
const bannerGone = () => (S.connOk === false ? setConn(false) : banner(""));
export function banner(text, stay = false) {
  const b = $("banner");
  b.textContent = text;
  b.classList.toggle("on", !!text);
  b.onclick = bannerGone;
  bannerStays = !!text && stay;
  b.onkeydown = null;
  delete b.dataset.conn;   // the connection status (setConn) counts itself down and is named "Retry"; any other text is not
  b.removeAttribute("tabindex");
  b.removeAttribute("title");
  b.removeAttribute("aria-label");
  b.setAttribute("role", "status");
  clearTimeout(bannerTimer);
  if (text && !stay) bannerTimer = setTimeout(() => { if (b.textContent === text) bannerGone(); }, 10000);
}

// the hub's words from a plugin (hub.banner, Plugin API 1.30): a single line, at most 300 characters, text only — it
// fades after 10 s, a click closes it; a banner held by the page itself ("no connection", "updated") is not covered;
// returns false also when the banner is not visible (narrow screen, session list)
export function say(text) {
  const line = [...String(text ?? "").replace(/\s+/g, " ").trim()].slice(0, 300).join("");
  // on a narrow screen in the session list (#/sessions) main is hidden and the banner lives in it: there is nothing to
  // show, so do not report it as shown
  if (!line || bannerStays || !$("banner").parentElement?.getClientRects().length) return false;
  banner(line);
  return true;
}

// the same status on a phone in the session list: a press tries right now, without changing the stream's usual retry
function connectNow(b) {
  if (!b) return;
  b.setAttribute("tabindex", "0");
  b.setAttribute("role", "button");
  b.setAttribute("title", t("web.main.feed_retry"));
  b.onclick = () => connect();
  b.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); connect(); } };
}

// a snapshot arrives on every hub event: when the connection is back, clear only our own status, not someone else's error.
// The status is "Connecting to the hub… · in N s" (backoff.js showWait): `wait.nextAt` is when the next attempt starts
function setConn(ok) {
  S.connOk = ok;
  account();
  const side = $("connection-side");
  if (side) {
    if (ok) side.textContent = ""; else showWait(side, wait.nextAt);
    side.classList.toggle("on", !ok);
    if (!ok) connectNow(side);
    else {
      side.onclick = side.onkeydown = null;
      side.removeAttribute("tabindex"); side.removeAttribute("title"); side.removeAttribute("aria-label");
      side.setAttribute("role", "status");
    }
  }
  const b = $("banner");
  if (!ok) {
    if (!b.dataset.conn) banner(t("web.api.no_conn"), true);   // already ours: only the time changes (showWait)
    showWait(b, wait.nextAt);
    b.dataset.conn = "1";
    connectNow(b);
  }
  else if (b.dataset.conn) banner("");
}

// "Settings" is the first item of the ☰ menu, with an account icon; the status light is on that item and on ☰ itself:
// green — connected; yellow — something worth knowing (a new version, a browser not answering, a plugin with a
// warning); red — no connection. What exactly is shown on the item's second line and in the tooltip
export function account() {
  const b = $("conn");
  if (!b) return;
  const notes = [];
  if (S.connOk && staleVersion()) notes.push(t("web.api.note_update"));
  const down = (S.snap?.browsers || []).filter((x) => !x.up && !x.idle).length;   // same as browsersDown() in browsers.js
  if (S.connOk && down) notes.push(down === 1 ? t("web.api.note_browser_down") : t("web.api.note_browsers_down", { count: down }));
  const warn = S.connOk ? (S.snap?.plugins || []).filter((p) => p.warning && !p.disabled).length : 0;
  if (warn) notes.push(t("web.api.note_plugins", { count: warn }));
  const state = S.connOk === undefined ? "wait" : !S.connOk ? "off" : notes.length ? "warn" : "ok";
  b.querySelector(".dot").dataset.s = state;
  const m = $("navmenu")?.querySelector(".dot");
  if (m) m.dataset.s = state;
  const ic = b.querySelector(".icon"), want = kit.icons.account || "";   // the account icon — a theme may have its own
  if ((ic.dataset.kit || "") !== want) { const n = icon("account"); n.dataset.kit = want; ic.replaceWith(n); }
  const said = [state === "off" ? t("web.api.state_off") : state === "wait" ? t("web.api.state_wait") : t("web.api.state_ok"), ...notes].join(" · ");
  const text = `${t("web.settings.tab.general")} · ${said}`;
  const lb = b.querySelector(".lb");
  if (lb.textContent !== said) lb.textContent = said;
  b.setAttribute("aria-label", text);
  b.title = text;
  b.setAttribute("aria-current", String(S.selected === ":settings"));
  b.onclick = () => { if (notes[0] === t("web.api.note_update")) localStorage.setItem("hub.settingsTab", "about"); location.hash = "#/settings"; };
}

// Left the page — close the stream: Chrome keeps the page in the back/forward cache together with the open
// /api/stream, and a browser has six connections per host — the sixth hub load waited for the feed (/api/timeline)
// forever. Came back with the "back" button — open the stream again, the server starts with a full snapshot
// wait: {fails — attempts in a row that did not get through, nextAt — when the next one starts, timer — that attempt}
// since — when the attempt under way started
let es = null;
const wait = { fails: 0, nextAt: 0, timer: null, since: 0 };
addEventListener("pagehide", () => { clearTimeout(wait.timer); wait.timer = null; es?.close(); es = null; });
addEventListener("pageshow", (e) => { if (e.persisted && !es) connect(); });
// the network is back (`fresh`: the pauses start over, an attempt that may hang on the dead route is replaced) or the
// tab is in front again — try right now; the feed on screen that did not load too. focus and visibilitychange come
// together: an attempt that started within 2 s is not started a second time
const kick = (fresh) => {
  if (S.connOk === false && (fresh || wait.timer || Date.now() - wait.since > 2000)) { if (fresh) wait.fails = 0; connect(); }
  retryFailed(fresh);
};
addEventListener("online", () => kick(true));
addEventListener("focus", () => kick(false));
addEventListener("visibilitychange", () => { if (!document.hidden) kick(false); });

// plugins' "new" (hub.addUnread): on every snapshot, when a plugin connects, and on hub.redraw. Each pass starts from
// the core's value (core_unread: the session rows from the snapshot are the same objects until the core sends them
// again), not from the plugin's previous answer; plugins run in the order they connected, the next one sees the
// previous one's answer
let applying = false;   // a plugin calls hub.redraw from its own addUnread — do not recurse
export function applyUnread() {
  if (!S.unread.length || applying) return;
  applying = true;
  try { applyAll(); } finally { applying = false; }
}
function applyAll() {
  for (const s of S.snap?.sessions || []) {
    s.core_unread ??= !!s.unread;
    s.unread = s.core_unread;
  }
  for (const { ext, fn } of S.unread) {
    for (const s of S.snap?.sessions || []) {
      try { const v = fn(s, S.snap); if (typeof v === "boolean") s.unread = v; }
      catch (err) { console.warn(t("web.ext.plugin_label", { name: ext }) + ": " + t("web.ext.unread_failed"), err); }
    }
  }
}

export function connect() {
  clearTimeout(wait.timer);
  wait.timer = null;
  wait.nextAt = 0;   // an attempt is under way: the status says so until it fails
  wait.since = Date.now();
  if (S.connOk === false) setConn(false);
  es?.close();
  // delta=2: a patch carries the changed items of a big section, not the section (stream-delta.js); a server without
  // it ignores the parameter and sends whole sections
  const me = es = new EventSource("/api/stream?delta=2");
  const take = () => {
    setConn(true);
    shareTextScale();   // "Files" learns the text size once the snapshot names its port
    applyUnread();
    adopt(S.snap);  // the stream has no feeds — only the ones the page already knows (timeline.js)
    if (reloadIfUpdated()) return;
    renderLive();   // do not break the text selection (state.js)
    sync(S.cur?.name || S.selected);   // without #/s/... the hub is shown — load its feed (otherwise "Loading the feed..." hung forever)
  };
  es.addEventListener("snapshot", (e) => { S.snap = JSON.parse(e.data); take(); });
  // after that the server sends only the parts of the snapshot that changed (it used to be hundreds of KB a second):
  // a new object, so "the snapshot changed" comparisons see the change; the unchanged parts are the same objects
  es.addEventListener("patch", (e) => {
    if (!S.snap) return;
    const next = applyPatch(S.snap, JSON.parse(e.data));
    if (!next) return connect();   // a patch was lost: a new stream starts with a full snapshot
    S.snap = next;
    take();
  });
  es.onopen = () => { wait.fails = 0; setConn(true); retryFailed(true); };
  es.onerror = () => {
    if (me !== es) return;
    // the browser reconnects on its own after a drop (CONNECTING), but a non-200 answer (502/503 while the core's page
    // restarts) closes the stream for good — the page stayed red until a manual reload. So we reconnect ourselves on
    // every failure, with the Fibonacci pause (backoff.js); the server starts a new stream with a full snapshot
    // (connect() and leaving the page cancel a pending retry)
    me.close();
    failedOnce(wait, connect);
    setConn(false);
    // a stream refused for the sign-in looks like any drop to EventSource: ask once what it was (core/pagepass.py)
    fetch("/api/signin").then(async (r) => signinGone(r.status, await r.json().catch(() => ({})))).catch(() => {});
  };
}

// the page's password (core/pagepass.py): 401 {code: "signin"} — this tab's sign-in is gone (the password changed, the
// cookie ran out): the page reloads into the sign-in form
export function signinGone(status, data) {
  if (status !== 401 || data?.code !== "signin") return false;
  location.reload();
  return true;
}

// a substantive refusal by the server (4xx except 408 and 429) — no point repeating it; otherwise (0 — no answer, 5xx,
// 408, 429) it may or may not have arrived: a retry with the same client_id is safe
export const refusal = (status) => status >= 400 && status < 500 && status !== 408 && status !== 429;

// with type, the body is sent as is with this Content-Type (an attachment file); without it, JSON
export async function post(path, body, type) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": type ?? "application/json", "X-Hub": "1" },
      body: type === undefined ? bare(JSON.stringify(body)) : body });
    const data = await r.json().catch(() => ({}));
    signinGone(r.status, data);
    return r.ok ? { data } : { error: data.error || t("web.api.rejected"), status: r.status, data };
  } catch {
    return { error: t("web.api.no_answer"), status: 0 };   // status 0 — no answer (a drop, the server is not responding)
  }
}

// the id for retrying a message: crypto.randomUUID exists only on https and localhost, and the page is also opened over plain http
export function newClientId() {
  const b = crypto.getRandomValues(new Uint8Array(16));
  return [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
}

// Input field transport (API 1.28): an owner message is POST /api/send with a client_id (its own or a new one); the
// core does not duplicate a repeat of the same id within a day. Receipt {ok, receipt, client_id, status, data?, error?}:
// receipt is accepted (the core accepted it; a repeat of an already accepted one too: there is one message; data.seq,
// data.id are the number and the message id), refused (the hub or the core refused with a reason, status 4xx — no point
// repeating), queued (API 1.29: in the hub's mailbox, the core did not decide within 3 s — the outcome comes later,
// receiptOf), unknown (no answer, a drop or a server failure — it may or may not have arrived: a retry with the same
// client_id is safe); status is the response code, 0 — no response.
export async function sendMessage(body) {
  const cid = typeof body?.client_id === "string" && body.client_id ? body.client_id : newClientId();
  try {
    const r = await fetch("/api/send", { method: "POST", headers: { "Content-Type": "application/json", "X-Hub": "1" },
      body: bare(JSON.stringify({ ...body, client_id: cid })) });
    const data = await r.json().catch(() => ({}));
    // a hub before 1.29 answered {ok} without receipt — that means "accepted"
    if (r.ok) return { ok: true, receipt: data?.receipt === "queued" ? "queued" : "accepted", client_id: cid, status: r.status, data };
    const refused = refusal(r.status);
    return { ok: false, receipt: refused ? "refused" : "unknown", client_id: cid, status: r.status,
             error: data.error || t(refused ? "web.api.rejected" : "web.api.no_answer") };
  } catch {
    return { ok: false, receipt: "unknown", client_id: cid, status: 0, error: t("web.api.no_answer") };
  }
}

// The outcome of a send, later (API 1.29): GET /api/send/receipt — {receipt: accepted (seq, id) | pending (in the
// mailbox) | refused (error, code?, seq) | unknown (the core never received it, it is not in the mailbox)}; if the
// server does not answer — {receipt: null}
export async function receiptOf(session, client_id) {
  try {
    const r = await fetch("/api/send/receipt?" + new URLSearchParams({ session, client_id }), { headers: { "X-Hub": "1" } });
    const data = await r.json().catch(() => ({}));
    return r.ok && data.receipt ? data : { receipt: null, status: r.status };
  } catch {
    return { receipt: null, status: 0 };
  }
}

// fields waiting for a receipt after queued: while there are any, the tab does not reload itself (a late refusal would have nowhere to be shown)
let awaiting = 0;
export function awaitingReceipt(delta) { awaiting = Math.max(0, awaiting + delta); }

// Whoever sent with the answer "queued" asks for the outcome with this: less and less often until the core decides.
// accepted — it is a message; refused (error) — the core said no; lost — the core has not known it for UNKNOWN_SURE and it is
// not in the outbox (so it was not received: a retry with the same client_id gives a single message); stuck — no decision
// in RECEIPT_GIVE_UP (stop asking, do not take the words back: the core delivers them when it frees up, a retry would double)
const RECEIPT_GIVE_UP = 10 * 60000;
const UNKNOWN_SURE = 60000;
export async function awaitDecision(session, client_id) {
  awaitingReceipt(1);
  try {
    const until = Date.now() + RECEIPT_GIVE_UP;
    let lost = 0;   // since when the core has not known it
    for (let pause = 1000; ; pause = Math.min(pause * 2, 10000)) {
      await new Promise((ok) => setTimeout(ok, pause));
      const got = await receiptOf(session, client_id);
      if (got.receipt === "accepted") return { receipt: "accepted", seq: got.seq };
      if (got.receipt === "refused") return { receipt: "refused", error: got.error };
      lost = got.receipt === "unknown" ? lost || Date.now() : 0;
      if (lost && Date.now() - lost >= UNKNOWN_SURE) return { receipt: "lost" };
      if (Date.now() >= until) return { receipt: "stuck" };
    }
  } finally {
    awaitingReceipt(-1);
  }
}

// A hub restart waits while the tab holds something that is not on the server (core/restart.py, page.held): a voice
// message or a plugin's recording (data-hub-hold) must not be cut off by a restart. What we hold is reported to the
// server on change and every 30 s while we hold it; a closed tab releases at once (pagehide), a crashed one is
// forgotten by the server after 150 s (HOLD_TTL)
const TAB = Math.random().toString(36).slice(2, 12);
let heldSent = "", heldAt = 0;
function pageHolds() {
  const out = recording() ? ["voice"] : [];
  const mark = document.documentElement.getAttribute("data-hub-hold");
  if (mark !== null) out.push(mark || "plugin");
  return out;
}
setInterval(() => {
  const holds = pageHolds(), key = holds.join(",");
  if (key === heldSent && (!key || Date.now() - heldAt < 30000)) return;
  heldSent = key;
  heldAt = Date.now();
  // it did not arrive (the server is restarting) — retry on the next tick, not after 30 s (a release should not wait 150 s)
  post("/api/hold", { tab: TAB, holds }).then((r) => { if (r.error && heldSent === key) heldSent = null; });
}, 2000);
// the tab was closed or reloaded — release at once (keepalive: the request survives leaving the page)
addEventListener("pagehide", () => {
  if (heldSent === "") return;   // null — the last send did not arrive: release too
  heldSent = "";
  fetch("/api/hold", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json", "X-Hub": "1" },
    body: JSON.stringify({ tab: TAB, holds: [] }) }).catch(() => {});
});

// The hub was updated (a new version of the page or extensions) — reload the tab; typed text is not lost: while
// there is anything in a field, only a banner is shown (pages update automatically).
let loaded = null;
// what a reload would lose: text in a textarea, text the owner typed into any other field (a card's field, a plugin's
// input: marked by an input event, so a value set without one does not count) and files attached to a message and not
// sent yet (the text of the message box is kept in localStorage, the files are not)
const typed = new WeakSet();
addEventListener("input", (e) => { if (e.target) typed.add(e.target); }, { capture: true, passive: true });
function unsaved() {
  if (Object.values(S.pending).some((l) => l?.length)) return true;
  for (const f of document.querySelectorAll("textarea, input, [contenteditable]:not([contenteditable=false])")) {
    if (f.tagName === "TEXTAREA") { if (f.value.trim()) return true; }
    else if (typed.has(f) && (f.tagName === "INPUT" ? /^(text|search|url|email|tel|password|number|)$/.test(f.type) && f.value.trim() : f.textContent.trim())) return true;
  }
  return false;
}
function reloadIfUpdated() {
  const v = S.snap.version;
  if (!v) return false;
  if (!loaded) { loaded = v; return false; }
  if (v === loaded) return false;
  // and while the tab holds a recording (voice, data-hub-hold — pageHolds): a reload would cut it off
  const typing = unsaved() || pageHolds().length > 0 || awaiting > 0;
  if (typing) { banner(t("web.api.updated"), true); return false; }
  location.reload();
  return true;
}
