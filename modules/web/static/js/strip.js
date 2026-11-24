// The "Waiting for you" strip above the input field (pinned): what needs you right now — a permission request, a
// question, an undelivered message, the queue. When empty it is not shown. The question and permission card stays in
// the feed (the question lives in the scroll); the strip gives it a short link: the decision is made only in the card,
// which has the reason field. Red is used here only for "Waiting for you".
import { el, btn, stepNode } from "./dom.js";
import { S, render, isRunner, runningStep } from "./state.js";
import { interrupt, stopping, stopExecutor, stoppingUntil } from "./dialogs.js";
import { post } from "./api.js";
import { inBubble } from "./outgoing.js";
import { t, CLOSED } from "./i18n.js";
import { needsOwner } from "./sidebar.js";
import { isFolded, foldable, foldToggle, openAsks } from "./askfold.js";
import { memberOf } from "./who.js";

const FAILED_FOR = 6 * 3600;   // undelivered older than six hours does not belong in the strip: it is history, not a task
const UNDO_FOR = 6000;         // "Remove" delays the cancel: this long you can change your mind
const REFUSED_AFTER = 10000;   // the cancel went out, yet the message is still queued — the core refused
const SHOWN_QUEUE = 3;         // queue rows in view, the rest sit under "N more"
// a line inserted mid-turn waits behind a step running this long — a row with a stop button (the owner's slice: "a step
// longer than 60 s"): above p75 of how long insertions wait to be read (26 s); 16% of insertions waited longer
const UNREAD_AFTER = 60;
const HEAD_QUEUE = t("web.strip.head_queue"), HEAD_FYI = t("web.strip.head_fyi");   // headings of the queue disclosures
const FYI_HINT = t("web.strip.fyi_hint");
const HIDDEN_KEY = "hub.failedHidden";
const phone = () => matchMedia("(max-width: 760px)").matches;

// id of the message being removed → {session, text, failed, done, timer}. Lives here, not in the row: rows are rebuilt
// on every snapshot, while the timer and the owner's choice are not
const cancels = new Map();
const openQueue = new Set();   // sessions where the queue is expanded ("N more ▸", on a phone "In queue · N ▸")
const sending = new Set();     // ids of undelivered messages already being resent: a second click makes no duplicate
const resendFailed = new Set();
const opened = new Set();      // ids of "FYI" messages expanded in full: rows are rebuilt on every snapshot, the choice is not
let hidden = null;             // ids of undelivered messages already resent: "Not delivered" no longer shows them

function hiddenIds() {
  if (!hidden) {
    try { hidden = new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]")); } catch { hidden = new Set(); }
  }
  return hidden;
}
function hide(id) {
  hiddenIds().add(id);
  // per browser, not per tab: what was removed with "Remove" does not come back after a reload; old ids are dropped,
  // they are not shown past FAILED_FOR anyway
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hidden].slice(-200))); } catch { /* no storage — until reload */ }
}

const stateOf = (it) => (S.snap.messages[it.id] || {});
const clip = (text, n) => { const t = String(text || "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n) + "…" : t; };
const argOf = (input = {}) => String(input.command ?? input.file_path ?? input.url ?? input.pattern ?? Object.values(input).find((v) => typeof v === "string") ?? JSON.stringify(input));

function toCard(s, sel) {
  const card = document.querySelector(sel) || [...document.querySelectorAll(".feed .card")].pop();
  card?.scrollIntoView({ block: "center" });
  card?.querySelector("input, button")?.focus({ preventScroll: true });
}

// "Asks permission: Bash, rm -rf build" — a short command (≤18 chars) is its own nowrap span, so an open row never breaks it
// midway; the text (and a copy of it) stays the translated string, the 13 languages are untouched. A long one wraps as before.
const NOWRAP_MAX = 18;   // the same threshold as the native clients
function permText(tool, raw) {
  const arg = clip(raw, 120);
  if (!arg || arg.length > NOWRAP_MAX) return t("web.strip.asks_permission", { tool, arg });
  const mark = "\u0000";
  const [head, ...rest] = t("web.strip.asks_permission", { tool, arg: mark }).split(mark);
  if (!rest.length) return t("web.strip.asks_permission", { tool, arg });
  const cmd = el("span", "nowrap", arg);
  return [head, cmd, rest.join(arg)];
}

function line(kind, label, text, title, ...actions) {
  const r = el("div", "strip-row");
  r.dataset.k = kind;
  const t = el("span", "t");
  t.append(...[].concat(text));   // a string or parts (a short command stays whole, see permText)
  if (title) t.title = title;
  r.append(...(label ? [el("span", "k", label)] : []), t, ...actions);
  return r;
}

// a message as the feed shows it: its label (the core's words in the hub's language, a button press) or its text; resend
// and cancel go by the text
const textOf = (it) => String(it.label || it.text || "");

// ——— queue: "Remove" is deferred, the real cancel goes out only after 6 seconds have passed and you did not change your mind ———
function startCancel(s, it) {
  const e = { session: s.name, text: it.text, failed: false, done: false, timer: null };
  cancels.set(it.id, e);
  e.timer = setTimeout(() => sendCancel(it.id), UNDO_FOR);
  render();
}
async function sendCancel(id) {
  const e = cancels.get(id);
  if (!e || e.done) return;
  e.timer = null;
  // left the session, or the message has already gone out by itself — do not cancel: safer to leave it queued than lose it
  const m = S.snap?.messages?.[id] || {};
  if (S.selected !== e.session || m.state !== "queued") { cancels.delete(id); render(); return; }
  e.failed = false;
  const r = await post("/api/queue/cancel", { session: e.session, id });
  if (r.error) e.failed = true; else { e.done = true; e.at = Date.now(); setTimeout(render, REFUSED_AFTER + 100); }
  render();
}
function undoCancel(id) {
  const e = cancels.get(id);
  if (e) clearTimeout(e.timer);
  cancels.delete(id);
  render();
}

function queueRow(s, it, first, total) {
  const m = stateOf(it), e = cancels.get(it.id), fyi = m.state === "aside";
  const label = first ? (fyi ? HEAD_FYI : HEAD_QUEUE) + (total > 1 ? ` · ${total}` : "") : "";   // one row: the heading alone, no "· 1"
  if (e) {
    return e.failed
      ? line("fail", label, t("web.strip.not_removed", { text: clip(textOf(it), 60) }), "", btn(t("web.strip.retry"), () => sendCancel(it.id), "quiet"))
      : line("queue", t("web.strip.removed"), clip(textOf(it), 70), "", btn(t("web.strip.undo"), () => undoCancel(it.id), "quiet"));
  }
  if (fyi) return fyiRow(it, label);
  const acts = [];
  if (s.state === "busy" && m.state === "queued" && m.injectable) {
    acts.push(btn(t("web.strip.send_now"), () => post("/api/inject", { session: s.name, id: it.id })));
  }
  if (m.state === "queued" && m.removable !== false) acts.push(btn(t("web.strip.remove"), () => startCancel(s, it), "quiet"));
  const r = line("queue", label, clip(textOf(it), 90), t("web.strip.queue_hint"), ...acts);
  if (s.state === "busy" && m.urgency != null) {
    const u = el("span", "strip-urg", t("web.strip.urgency", { percent: Math.round(m.urgency * 100) }));
    u.dataset.level = m.urgency >= 0.6 ? "high" : m.urgency >= 0.35 ? "mid" : "low";
    u.title = t("web.strip.urgency_hint");
    r.querySelector(".t").after(u);
  }
  return r;
}

// "FYI" (hub send --fyi): goes out by itself with the session's next message, the session is not woken for it — nothing
// is expected from the owner, so no buttons ("Remove" read as "remove it by hand"). The full text is shown: before
// delivery there is nowhere else to read it; one line with "…" is visible, a click or Enter expands it
function fyiRow(it, label) {
  const r = line("queue", label, "", "");
  // a real button: Enter and Space belong to it, not to voice input (which catches Space on the document and skips buttons)
  const t = el("button", "t", textOf(it).trim());
  t.type = "button";
  t.title = FYI_HINT;
  t.dataset.id = it.id;
  r.querySelector(".t").replaceWith(t);
  // in place, without a redraw: focus and hover stay; a rebuild from the snapshot takes the expanded state from opened.
  // If the row is already on screen but is not this one (it was rebuilt during the delayed click), look it up again;
  // if it is not yet inserted, use this one
  const set = (open) => {
    if (open) opened.add(it.id); else opened.delete(it.id);
    const b = t.isConnected || !r.parentNode ? t : document.querySelector(`.strip-row .t[data-id="${CSS.escape(it.id)}"]`);
    b?.closest(".strip-row").classList.toggle("open", open);
    b?.setAttribute("aria-expanded", String(open));
  };
  set(opened.has(it.id));
  // mouse — with a delay: a double click selects a word rather than expanding or collapsing; selecting this row's
  // text is not a click either
  const picked = () => { const sel = getSelection(); return !!sel && !sel.isCollapsed && sel.containsNode(t, true); };
  let timer = null;
  t.addEventListener("click", (ev) => {
    clearTimeout(timer);
    if (ev.detail === 0) set(!opened.has(it.id));   // from the keyboard
    else if (ev.detail === 1) timer = setTimeout(() => { if (!picked()) set(!opened.has(it.id)); }, 300);
  });
  t.addEventListener("keydown", (ev) => { if (ev.repeat && (ev.key === "Enter" || ev.key === " ")) ev.preventDefault(); });
  return r;
}

function toggle(key, label, open) {
  const b = el("button", "strip-toggle", `${label} ${open ? "▾" : CLOSED}`);
  b.type = "button";
  b.setAttribute("aria-expanded", String(open));
  b.addEventListener("click", () => { if (open) openQueue.delete(key); else openQueue.add(key); render(); });
  return b;
}

// head — "In queue" or "FYI" (hub send --fyi: waits at the session, does not start a turn) — each has its own disclosure
function queueRows(s, queued, head = HEAD_QUEUE) {
  const key = head === HEAD_QUEUE ? s.name : `${s.name}\n${head}`;
  const open = openQueue.has(key), n = queued.length, sig = queued.map((x) => x.id + stateOf(x).state + (cancels.get(x.id) ? "c" + !!cancels.get(x.id).failed : "")
    + (s.state === "busy" ? (stateOf(x).urgency ?? "") + stateOf(x).injectable : "")).join();
  const base = `q${head}${sig}|${s.state}|${open}`;
  if (phone()) {
    // on a phone the queue must not cover the feed: one row "In queue · N ▸" that expands in place when tapped
    const sum = { sig: base + "p", make: () => { const r = el("div", "strip-row strip-sum"); r.dataset.k = "queue"; r.append(toggle(key, n > 1 ? `${head} · ${n}` : head, open)); return r; } };
    return open ? [sum, ...queued.map((it, i) => ({ sig: base + "p" + i, make: () => queueRow(s, it, false, n) }))] : [sum];
  }
  const shown = open ? queued : queued.slice(0, SHOWN_QUEUE);
  const out = shown.map((it, i) => ({ sig: base + i, make: () => queueRow(s, it, i === 0, n) }));
  if (n > SHOWN_QUEUE) {
    out.push({ sig: base + "more", make: () => { const r = el("div", "strip-row strip-more"); r.dataset.k = "queue"; r.append(toggle(key, open ? t("web.strip.collapse") : t("web.strip.more", { count: n - SHOWN_QUEUE }), open)); return r; } });
  }
  return out;
}

// ——— "Resend": one new message with the same fields; the old one is no longer shown under "Not delivered" ———
async function resend(s, it) {
  if (sending.has(it.id)) return;
  sending.add(it.id);
  const m = stateOf(it);
  const files = Array.isArray(m.attachments) && m.attachments.length ? m.attachments : (it.attachments || []);
  const r = await post("/api/send", { session: s.name, text: it.text, attachments: files, quote: it.quote ?? null, reply_to: it.reply_to ?? null, wake: it.wake === true,
    ...(it.tasks?.length ? { tasks: it.tasks } : {}) });
  sending.delete(it.id);
  if (r.error) resendFailed.add(it.id); else { resendFailed.delete(it.id); hide(it.id); }
  render();
}

// what is in the strip now: rows in order of importance; the "signature" is there to avoid rebuilding unchanged nodes
function rows(s) {
  const out = [];
  // "Hide" questions (askfold.js) hides the question rows; permissions, tool connection, the queue and undelivered
  // messages are not questions and stay
  for (const q of s.pending || []) {
    if (q.tool === "AskUserQuestion") {
      if (isFolded(s)) continue;
      const first = ((q.input || {}).questions || [])[0] || {};
      out.push({ sig: "ask" + q.request_id, make: () => {
        const r = line("wait", t("web.strip.waiting"), t("web.strip.question", { text: clip(first.question, 90) }), first.question,
          btn(t("web.strip.to_question"), () => toCard(s, `.ask-card[data-session="${CSS.escape(s.name)}"][data-ask="${CSS.escape(q.request_id)}"]`)));
        r.dataset.q = "1";   // a question row: the first one carries the "Hide" toggle (refreshStrip)
        return r;
      } });
    } else {
      // a permission is decided only in the card: it has the reason field, while a one-tap approval from the strip is blind
      const arg = argOf(q.input);
      out.push({ sig: "perm" + q.request_id, make: () => line("wait", t("web.strip.waiting"), permText(q.tool, arg), arg,
        btn(t("web.strip.to_request"), () => toCard(s, ".feed .card"))) });
    }
  }
  for (const r of needsOwner(s)) {
    out.push({ sig: "tool" + r.tool, make: () => line("wait", t("web.strip.waiting"), t("web.strip.asks_connect", { tool: r.tool }), r.why || "", btn(t("web.strip.view"), () => toCard(s, ".feed .card"))) });
  }
  // words passed to the mini-hub (core/views.py: relayed) wait in its strip, not here: it is not this session's queue
  const live = (S.snap.timelines[s.name] || []).filter((it) => !it.relayed);
  const now = Date.now() / 1000;
  const lost = live.filter((it) => it.kind === "in" && !it.sender && memberOf(it) === null && stateOf(it).state === "failed" && now - it.ts < FAILED_FOR && !hiddenIds().has(it.id));
  const failed = lost[lost.length - 1];
  if (failed) {
    const err = resendFailed.has(failed.id), more = lost.length > 1 ? t("web.strip.failed_more", { count: lost.length - 1 }) : "";
    out.push({ sig: "fail" + failed.id + err + lost.length, make: () => {
      const b = btn(err ? t("web.strip.retry") : t("web.strip.resend"), () => resend(s, failed));
      // not needed — remove all undelivered messages of this session without sending anything (users asked for a plain drop)
      const drop = btn(t("web.strip.remove"), () => { for (const it of lost) hide(it.id); render(); }, "quiet");
      return line("fail", t("web.strip.failed"), t("web.strip.failed_text", { text: clip(textOf(failed), 70), more }), stateOf(failed).reason || "", b, drop);
    } });
  }
  for (const [id, e] of cancels) if (e.done && stateOf({ id }).state !== "queued") cancels.delete(id);   // the snapshot caught up with the cancel
  // the core refused (a refusal comes not as a reply to the request but as silence) — the row returns as "Not removed"
  for (const e of cancels.values()) if (e.done && Date.now() - e.at > REFUSED_AFTER) { e.done = false; e.failed = true; }
  // what was typed is already with the session: a command (/compact) runs for minutes — that is not a queue
  for (const it of live.filter((x) => x.kind === "in" && stateOf(x).state === "typing" && !inBubble(x, s.name))) {
    const label = stateOf(it).command ? t("web.strip.running") : t("web.strip.sending");
    out.push({ sig: "run" + it.id + label, make: () => line("queue", label, clip(textOf(it), 90), "") });
  }
  const unread = unreadRow(s, live);
  if (unread) out.push(unread);
  const waits = (state) => live.filter((it) => it.kind === "in" && stateOf(it).state === state && !cancels.get(it.id)?.done);
  const queued = waits("queued"), aside = waits("aside");
  for (const id of opened) if (stateOf({ id }).state !== "aside") opened.delete(id);   // delivered or withdrawn — forget it
  if (queued.length) out.push(...queueRows(s, queued));
  if (aside.length) out.push(...queueRows(s, aside, HEAD_FYI));
  return out;
}

// What was inserted mid-turn (--now, the watchdog) and not read yet (the core: unread) while one step runs past a minute:
// Claude Code reads stdin only between steps, so words like "stop" wait for the step. A row says so, with a button that
// does not wait: an executor is stopped (its tree is killed, the unread lines withdrawn), another session's turn is
// interrupted (Esc). Below the minute nothing is shown (insertions are usually read in seconds), only a timer for the
// row to come up by itself. Only while busy: in dialog the session waits for the owner's card, not for a step, and Esc
// there would drop the question
let unreadTimer = null;
function unreadRow(s, live) {
  clearTimeout(unreadTimer);
  if (s.state !== "busy") return null;
  const ids = live.filter((it) => it.kind === "in" && !it.to && stateOf(it).inserted_unread).map((it) => it.id);
  const step = ids.map((id) => runningStep(s.name, stateOf({ id }).delivered_at)).find(Boolean);
  if (!step) return null;
  const left = step.ts + UNREAD_AFTER - Date.now() / 1000;
  if (left > 0) { unreadTimer = setTimeout(render, left * 1000 + 50); return null; }
  // pressed: "Stopping…" until the snapshot moves on, the same mark as the header's button (one press, one request)
  // and once that mark expires the button is pressable again (dialogs.js lets a second request through) — redraw then
  const runner = isRunner(s), until = runner ? stoppingUntil(s) : (stopping.get(s.name) || 0) + 15000;
  const asked = Date.now() < until;
  if (asked) unreadTimer = setTimeout(render, until - Date.now() + 50);
  return { sig: `unread${ids.join(",")}${step.id}${runner}${asked}`, make: () => {
    const label = asked ? t("web.main.stopping") : runner ? t("web.main.stop_executor") : t("web.strip.interrupt");
    const b = btn(label, () => (runner ? stopExecutor(s) : interrupt(s)).then(render), runner ? "danger" : "");
    if (asked) b.disabled = true;
    b.title = runner ? t("web.main.stop_executor_hint") : t("web.main.stop_hint");
    const r = line("unread", t("web.strip.unread"), "", t("web.feed.unread_hint"), b);
    // the step first: on a narrow row the end is cut, and how long it runs is the point
    r.querySelector(".t").append(stepNode(step, true), document.createTextNode(" — " + t("web.strip.unread_text")));
    return r;
  } };
}

// host — a node in the dock; redrawn only when its contents change, so hover and focus are not lost on every snapshot
export function refreshStrip(host, s) {
  if (!host) return;
  for (const [id, e] of cancels) if (e.session !== s.name) { clearTimeout(e.timer); cancels.delete(id); }   // moved to another session — the cancel is not sent
  const list = rows(s);
  const fold = foldable(s);
  const sig = list.map((x) => x.sig).join("|") + (fold ? `|fold${isFolded(s)}${openAsks(s)}` : "");
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  const wasShown = host.childElementCount > 0;   // the strip was already on screen: rebuilding rows does not flicker
  host.hidden = !list.length && !fold;
  host.replaceChildren();
  if (!list.length && !fold) return;
  const box = el("div", "strip");
  box.setAttribute("role", "region");
  box.setAttribute("aria-label", t("web.strip.waiting"));
  const made = list.map((x) => x.make());
  if (fold) {
    // questions are hidden — one short row replaces their rows; the toggle is one per block, at the end of the first
    // "Waiting for you" row (after "To the question"). There is no row even without folding if the questions are only
    // shown to the hub (s.shown): the same short row serves as the carrier
    if (isFolded(s) || !made.some((r) => r.dataset.q)) {
      const r = el("div", "strip-row");
      r.dataset.k = "wait";
      r.dataset.q = "1";
      r.append(el("span", "k", t("web.strip.waiting")), el("span", "t", t("web.fold.waiting", { count: openAsks(s) })));
      made.unshift(r);
    }
    made.find((r) => r.dataset.q)?.append(foldToggle(s));
  }
  box.append(...made);
  host.append(box);
  // "empty → present": fade in gently once (css .strip-host.enter); the class is removed at the end so it can fire again
  if (!wasShown) { host.classList.add("enter"); host.addEventListener("animationend", () => host.classList.remove("enter"), { once: true }); }
}
