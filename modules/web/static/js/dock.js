// The bottom of a session: question cards, the task list, the ❯ field with attachments, the status line (model, mode, turn).
import { el, lookSig, placeNodes } from "./dom.js";
import { slotNodes } from "./ext.js";
import { S, isRunner, render, sessionName } from "./state.js";
import { answered, askCard, permCard, specCard, toolCard } from "./cards.js";
import { dictationIn } from "./dictate.js";
import { needsOwner } from "./sidebar.js";
import { numberDlg, STOPPABLE } from "./dialogs.js";
import { effortControl, effortLabel, modelControl, modelLabel } from "./modelpicker.js";
import { fileLink, plainText } from "./markdown.js";
import { limitsLine } from "./limits.js";
import { refreshStrip } from "./strip.js";
import { isFolded } from "./askfold.js";
import { buildComposer } from "./composer.js";
import { spaceHint } from "./voice.js";
import { moreMenu } from "./menu.js";
import { banner, post } from "./api.js";
import { carry, clear as clearPicks, list as pickedList, restore as restorePicks, tray as pickTray } from "./mention.js";
import { locale, t } from "./i18n.js";

// the dock is rebuilt only when its composition changes: another session, or the session closed/crashed; the input
// field, the strip, the tasks and the status line update themselves on every snapshot (refreshTodosAndStatus)
// without losing focus or text
export function dockKey(s) {
  return [s.name, ["closed", "failed"].includes(s.state) ? s.state : "live"].join("|");
}

export function dock(s) {
  const d = el("div", "dock"), inner = el("div", "dock-inner");
  d.append(inner);
  inner.append(el("div", "todos"));
  const strip = el("div", "strip-host");   // the "Waiting for you" strip (strip.js): updates with the snapshot, does not recreate the field
  strip.setAttribute("aria-live", "polite");
  inner.append(strip);
  if (["closed", "failed"].includes(s.state)) {
    inner.append(el("p", "hint", s.state === "failed"
      ? t("web.dock.session_failed") : t("web.dock.session_closed")));
  } else {
    inner.append(composer(s));
  }
  // the row's words are the hub's own (model names, effort levels, chips from the catalog): the browser's page translation
  // takes a page with English chat text for English and renders them as other words
  const status = el("div", "status");
  status.translate = false;
  inner.append(status);
  return d;
}

// question and permission cards sit at the end of the feed, inside its scroll (so the owner can look at what came
// before the question). The feed is rebuilt on every snapshot, the card is not: the same node (the chosen option and
// the typed "own answer" are not lost) while the question stays the same
const cardCache = new Map();
// the question was taken off without using this card (answered from Telegram or another device, or the session
// withdrew the question), while the card holds typed text — "Other...", a refusal reason: the card does not vanish
// with it but stays frozen — it cannot be answered from, the text can be moved to the input field. Without typed text
// the card goes away as before
const frozen = new Map();   // card key -> node, until "Close" or "To the input field"
function typed(card) {
  return [...card.querySelectorAll("textarea, input[type=text]")].map((x) => x.value.trim())
    .concat([...card.querySelectorAll(".dictate-note[data-said]")].map((x) => x.dataset.said))   // dictated, not inserted
    .filter(Boolean).join("\n\n");
}
// "Already answered" — the feed has the core's answer row for this request (an answer to the card from any client);
// otherwise "Question closed": which is also true for an answered one whose feed is not loaded (a card shown by the
// hub, a tool request)
function answeredIn(k) {
  const [name, kind, ...rest] = k.split(":"), rid = rest[rest.length - 1];
  if (kind === "tool") return false;
  const lines = [name, kind === "shown" ? rest[0] : name].flatMap((n) => S.snap.timelines?.[n] || []);
  return lines.some((it) => it.kind === "answer" && it.request_id === rid);
}
// our own answer was recorded by the core: the session's answered[] has the card's request with its client_id (cards.js deliver)
function ours(c) {
  if (!c.dataset.cid) return false;
  const to = (S.snap.sessions || []).find((x) => x.name === c.dataset.to);
  return !!to?.answered?.some((r) => r.request_id === c.dataset.rid && r.client_id === c.dataset.cid);
}
function freeze(s, k, card) {
  card.dataset.frozen = "1";   // askCard/permCard/toolCard: an answer from a frozen card is not sent (cards.js)
  card.classList.add("frozen");
  card.classList.remove("sent");   // collapsed while the answer is in flight: the typed text is visible again
  card.querySelector(".sent-line")?.remove();
  // the dictation microphone and its "Retry"/"Download" stay as they are: a recording that runs or failed is finished
  // (the microphone hides itself afterwards, the text does not go into the frozen field — dictate.js)
  const dictation = (x) => x.matches(".dictate") || !!x.closest(".dictate-note");
  for (const x of card.querySelectorAll("input, textarea, button")) {
    if (x.matches("textarea, input[type=text]")) x.readOnly = true;   // the text can still be selected and copied
    else if (!dictation(x)) x.disabled = true;
  }
  for (const b of card.querySelectorAll(".btns, .card-x, .other-row .btn")) if (!dictation(b)) b.hidden = true;
  for (const b of card.querySelectorAll(".dictate")) b.hubPaint?.();   // the field is read-only now: no microphone, unless it records
  // the header is no longer "waiting for your answer" but what became of the question (the mark is updated in ownerCards)
  card.querySelector(".card-head h3")?.classList.add("frozen-note");
  const bar = el("div", "frozen-bar");
  bar.setAttribute("role", "status");
  const move = el("button", "btn primary small", t("web.cards.frozen_to_input"));
  move.type = "button";
  move.addEventListener("click", () => {
    const text = typed(card);
    S.drafts[s.name] = (S.drafts[s.name] ? S.drafts[s.name] + "\n" : "") + text;
    frozen.delete(k);
    render();
    document.getElementById("msg")?.focus();   // this session's field (composerProps, id "msg")
  });
  const close = el("button", "btn small", t("web.cards.frozen_close"));
  close.type = "button";
  close.addEventListener("click", () => { frozen.delete(k); render(); });
  bar.append(move, close);
  if (!card.querySelector(".frozen-note")) bar.prepend(el("span", "frozen-note"));   // a card without a header
  card.append(bar);   // at the bottom, where the answer buttons were (they are hidden)
  return card;
}
// the field is aimed at a question card: the next message from it is a comment on that card (S.cardAim — {view, key},
// key as in the card cache). The aim holds only while its card is open, on screen (not folded) and still takes an
// answer; the card answered or closed by its own buttons takes the aim off (cards.js __done)
function aimable(k) {
  const c = cardCache.get(k);   // not built yet (the field is drawn before the feed) — still a target
  return !answered.has(k) && !(c && (c.dataset.sending || c.dataset.frozen));
}
function aimOf(s) {
  const a = S.cardAim;
  return a && a.view === s.name && !isFolded(s) && askKeys(s).some(([k]) => k === a.key) && aimable(a.key) ? a.key : null;
}
// aimed in this feed, but the card went away without the owner's answer from here (answered in Telegram, withdrawn): the
// words must not go as a plain message — with one other card open the core takes them for that card's answer
const staleAim = (s) => !!S.cardAim && S.cardAim.view === s.name && !isFolded(s) && !aimOf(s);
// the open question cards of the session's page, in the order they stand: [cache key, request]
function askKeys(s) {
  return (s.pending || []).filter((r) => r.tool === "AskUserQuestion").map((r) => [`${s.name}:req:${r.request_id}`, r])
    .concat((s.shown || []).map((q) => [`${s.name}:shown:${q.session}:${q.request_id}`, q]));
}
// what a card is called on its target button: the question's short header, else the start of the question
function aimLabel(req) {
  const q = (req.input?.questions || [])[0] || {};
  const text = String(q.header || q.question || "").replace(/\s+/g, " ").trim();
  return text.length > 24 ? text.slice(0, 23).trimEnd() + "…" : text;
}
// the marked options of a card, as the owner sees them on it
const picksOf = (k) => [...(cardCache.get(k)?.querySelectorAll("input:checked") || [])].map((x) => x.value).join(", ");
// the owner points at a card: it replaces a "Reply" target, as "Reply" replaces the aim (feed.js replyTo); the pick
// waiting on the card the aim leaves is unmarked by the render (ownerCards)
function aim(s, k) {
  const was = aimOf(s), c = cardCache.get(k);
  S.cardAim = was === k ? null : { view: s.name, key: k };
  if (S.cardAim && S.replyTarget?.view === s.name) S.replyTarget = null;
  if (S.cardAim && c && !c.__aimWatch) { c.__aimWatch = true; c.addEventListener("change", () => { if (S.cardAim?.key === k) render(); }); }
  render();
  if (S.cardAim) document.querySelector("#msg")?.focus();
}

export function ownerCards(s) {
  const want = [...needsOwner(s).map((r) => [`${s.name}:tool:${r.tool}`, () => toolCard(s, r)])];
  // a spec of its own or of its executor waits for the owner's "Accept" (core/specmode.py)
  for (const x of S.snap.sessions || []) {
    if (x.spec?.ready && (x.name === s.name || x.raised_by === s.name)) want.push([`${s.name}:spec:${x.spec.task}`, () => specCard(s, x)]);
  }
  // every open question gets its own card, so "To the question" leads to the right one; a permission request — only the
  // first of the open ones: one decision at a time
  const asks = new Set();   // keys of the question cards — "Hide" hides them (askfold.js)
  (s.pending || []).forEach((req, i) => {
    if (i === 0 || req.tool === "AskUserQuestion") {
      want.push([`${s.name}:req:${req.request_id}`, () => (req.tool === "AskUserQuestion" ? askCard(s, req) : permCard(s, req))]);
      if (req.tool === "AskUserQuestion") asks.add(`${s.name}:req:${req.request_id}`);
    }
  });
  // questions of sessions that the hub showed in its own chat (hub question show): the card's answer goes to the session itself
  for (const q of s.shown || []) {
    want.push([`${s.name}:shown:${q.session}:${q.request_id}`, () => askCard(s, q)]);
    asks.add(`${s.name}:shown:${q.session}:${q.request_id}`);
  }
  for (const k of [...cardCache.keys()]) {
    if (!k.startsWith(s.name + ":") || want.some(([w]) => w === k)) continue;
    const c = cardCache.get(k);
    // our own answer arrived (got — a receipt or the snapshot's answered[]) or our own "close" (closing) — not a
    // freeze: the typed text went out with the answer. If the question was taken off while our answer is still in
    // flight (the hub is not responding, it was answered from Telegram), the card freezes with the typed text and goes
    // away when the answer arrives (below); if it is refused, the reason is shown in a strip (cards.js deliver)
    if (ours(c)) c.dataset.got = "1";
    if (!c.dataset.got && !c.dataset.closing && (typed(c) || dictationIn(c))) frozen.set(k, freeze(s, k, c));
    cardCache.delete(k);
  }
  // the question came back (the same request is open again) — a live card instead of the frozen one; our own answer
  // arrived — the frozen one is not needed; the session is gone — so are its cards
  if (frozen.size) {
    const names = new Set((S.snap.sessions || []).map((x) => x.name)), wanted = new Set(want.map(([w]) => w));
    for (const [k, c] of [...frozen]) {
      if (ours(c)) c.dataset.got = "1";
      if (wanted.has(k) || c.dataset.got || !names.has(k.split(":")[0])) frozen.delete(k);
    }
  }
  // the answer went out — the card is collapsed into a row with the answer (cards.js deliver) until the core's
  // snapshot; the core took the question off — the card goes away, the mark is no longer needed
  for (const k of [...answered]) if (k.startsWith(s.name + ":") && !want.some(([w]) => w === k)) answered.delete(k);
  const hide = isFolded(s);
  const cards = want.filter(([k]) => !(hide && asks.has(k)))
    .map(([k, make]) => {
      // key — scroll anchor like a feed row (main.js rowAt): reading the middle of a tall card, the place is kept by the
      // card itself; without it rows changing above moved the card (the page jumped up, then down)
      if (!cardCache.has(k)) { const c = make(); c.dataset.key = "card:" + k; cardCache.set(k, c); }
      return cardCache.get(k);
    })
    .concat([...frozen].filter(([k]) => k.startsWith(s.name + ":")).map(([k, c]) => {
      // the mark is set on every render: the answer row arrives with the feed (/api/timeline), after the snapshot
      // without the question
      c.querySelector(".frozen-note").textContent = answeredIn(k) ? t("web.cards.frozen_answered") : t("web.cards.frozen_closed");
      return c;
    }));
  const on = aimOf(s);   // the card the field is aimed at is outlined
  for (const c of cards) {
    const mine = !!on && c.dataset.key === "card:" + on, b = c.querySelector(".card-aim");
    // the aim left a one-question card by any way (a reply, another feed's card): its waiting pick is unmarked, so a
    // tap on the same option answers again
    if (!mine && c.classList.contains("aimed") && c.classList.contains("instant") && !c.dataset.sending)
      for (const x of c.querySelectorAll("input[type=radio]:checked")) x.checked = false;
    c.classList.toggle("aimed", mine);
    if (b) {
      const k = c.dataset.key.slice(5);
      b.setAttribute("aria-pressed", mine ? "true" : "false");
      c.__aim = () => aim(s, k);
      c.__done = () => { if (S.cardAim?.key === k) { S.cardAim = null; render(); } };   // its own buttons answered it
    }
  }
  return cards;
}

// tasks and status change on every snapshot, the field does not: update them separately without recreating the field
export function refreshTodosAndStatus(dockEl, s) {
  // plugin slots above the input field (ext.js, "session.composer")
  let host = dockEl.querySelector(".slot-host.composer");
  if (!host) { host = el("div", "slot-host composer"); dockEl.querySelector(".dock-inner").prepend(host); }
  placeNodes(host, slotNodes("session.composer", { session: s }));
  refreshStrip(dockEl.querySelector(".strip-host"), s);
  const box = dockEl.querySelector("form.composer");
  if (box) box.__composer.update(composerProps(s));   // hint, reply target, where it goes: from the fresh session
  const todos = dockEl.querySelector(".todos");
  placeBuilt(todos, (s.todos || []).map((t) => {
    const mark = t.status === "completed" ? "☑" : t.status === "in_progress" ? "◐" : "☐";
    const cls = t.status === "completed" ? "t-done" : t.status === "in_progress" ? "t-now" : "";
    return el("div", cls, `${mark} ${t.status === "in_progress" && t.activeForm ? t.activeForm : t.content}`);
  }));
  const status = dockEl.querySelector(".status");
  const info = s.info || {}, u = s.usage;
  const live = !["closed", "failed"].includes(s.state);
  // left: the model and effort (buttons for a live session, text for a closed one), the folder (the name; the path on
  // hover) and the last turn's summary; right: the limits
  const stat = el("span", "stat-main", live ? "" : [info.model, info.effort].filter(Boolean).join(" · "));
  // the last turn's summary is not in the line but in the tooltip (owner decision: hide it)
  const last = u && u.duration_ms ? t("web.dock.last_turn", { seconds: Math.round(u.duration_ms / 1000) }) + (u.turns ? t("web.dock.steps", { count: u.turns }) : "")
    + (u.tokens_in || u.tokens_out ? t("web.dock.tokens", { input: kTok(u.tokens_in), output: kTok(u.tokens_out) }) : "") : "";
  stat.title = [s.cwd, last].filter(Boolean).join("\n");
  const left = el("span", "stat-left");
  // the model by its full name, a click opens the model menu; outside stat-left, like the workers chip: that one
  // clips its edges and the upward menu would not fit; next to it is the reasoning level, built the same way
  const mod = live ? modelControl(s) : null, eff = live ? effortControl(s) : null;
  left.append(stat);   // empty (no effort) — hidden by the style, the tooltip with the folder and the turn summary stays
  // the session's folder, short (~/.../parent/folder), the full path on hover; a click opens it in "Files" (the folder
  // matters enough to always show, even abbreviated)
  if (s.cwd) {
    const home = String(s.cwd).replace(/^\/(?:Users|home)\/[^/]+/, "~");
    const segs = home.split("/").filter(Boolean);
    const a = fileLink(s.cwd, s.cwd, null, s.name);
    a.className = "stat-dir";
    a.replaceChildren(el("span", "dir-long", segs.length > 3 ? `${segs[0] === "~" ? "~/" : "/"}…/${segs.slice(-2).join("/")}` : home),
      el("span", "dir-short", segs[segs.length - 1] || home));   // narrow (phone) — only the folder name
    // when narrow, the model, effort and turn summary are not in the line — they are in the folder's tooltip
    a.title = [t("web.dock.open_in_files", { path: s.cwd }), [(s.model || info.model) && modelLabel(s), (s.effort || info.effort) && effortLabel(s)].filter(Boolean).join(" · "), last].filter(Boolean).join("\n");
    left.append(a);
  }
  const ctx = contextMeter(s);
  if (ctx) left.append(ctx);   // separators come from CSS between the parts: a part squeezed to zero leaves no "·" behind (phone)
  const par = parallelChip(s);
  // specs on (a person's /spec on, core/specmode.py): its big tasks start with a spec; /spec off turns them off
  const spec = s.specs_on ? el("span", "stat-spec", t("web.dock.specs_on")) : null;
  if (spec) spec.title = t("web.dock.specs_on_title");
  const lim = limitsLine();   // right: .status pushes the edges apart
  placeBuilt(status, [...[par, spec, mod, eff].filter(Boolean), left, ...(lim ? [lim] : [])]);
}

// tasks and the status line are built anew on every snapshot, mostly the same: the page is changed only when the built
// markup differs from the last one put in (not from the live one: a theme marks its nodes; another look puts them in
// anew). The same markup has the same handlers — they take only the session name and what the markup shows (an open
// menu is a different markup)
function placeBuilt(host, nodes) {
  const html = lookSig() + "\0" + nodes.map((n) => n.outerHTML).join("");
  if (host.__built === html) return;
  host.__built = html;
  host.replaceChildren(...nodes);
}

// the own number of workers of a specialist or a direction, a status bar chip configured by clicking: "workers: 3" /
// "self"; the menu opens upward — default (the shared parallelism), self, 1..., other.
// Who may set it and what takes effect is decided by the core (core/minihub.limit); a refusal is shown in words
const PAR_CHOICES = [1, 2, 3, 4, 5, 10], OWN_MAX = 20;   // OWN_MAX is the same as core/minihub.OWN_MAX
function parallelChip(s) {
  const lead = s.kind !== "hub" && (s.role === "agent" || s.backlog);
  if (!lead || ["closed", "failed", "removed"].includes(s.state) || S.snap?.parallel == null) return null;
  const hubN = S.snap.parallel, own = s.parallel ?? null, n = own ?? hubN, live = s.executors_live || 0;
  const solo = !!s.solo_auto || n === 0;
  const count = live ? t("web.dock.par_count", { live, n }) : String(n);
  const set = async (v) => { const r = await post("/api/session/parallel", { session: s.name, n: v }); if (r.error) banner(r.error); };
  const mode = s.solo_auto ? t("web.dock.par_solo_auto", { why: s.solo_auto })
    : n === 0 ? t("web.dock.par_solo")
    : s.minihub ? t("web.dock.par_minihub", { count: n, dirty: s.minihub_dirty ? `; ${s.minihub_dirty}` : "" })
    : s.minihub_pending ? t("web.dock.par_pending", { why: s.minihub_pending }) : t("web.dock.par_upto", { count: n });
  const items = [{ text: mode }, { sep: true },
    { label: t("web.dock.par_default", { n: hubN === 0 ? t("web.dock.par_solo_word") : hubN }), checked: own === null, title: t("web.dock.par_default_title"), onClick: () => set("default") },
    { label: t("web.dock.par_solo_item"), checked: own === 0, title: t("web.dock.par_solo_title"), onClick: () => set(0) }];
  for (const k of [...new Set([...PAR_CHOICES, ...(own > 0 ? [own] : [])])].sort((a, b) => a - b))
    items.push({ label: t("web.dock.par_n", { count: k }), checked: own === k, onClick: () => set(k) });
  items.push({ label: t("web.dock.par_other"), onClick: () => askNumber(s, n, set) });
  const m = moreMenu("par:" + s.name, items, solo ? t("web.dock.par_solo_word") : t("web.dock.par_button", { value: count }), { up: true });
  m.classList.add("stat-par");
  const b = m.querySelector(".menu-btn");
  b.className = "linkbtn menu-btn";
  if (own !== null) b.dataset.own = "1";
  if (!solo) b.replaceChildren(el("span", "par-long", t("web.dock.par_long")), el("span", "par-short", t("web.dock.par_short")), count);   // narrow — "wrk. 3"
  b.title = s.solo_auto ? t("web.dock.par_title_auto", { why: s.solo_auto }) : own === null ? t("web.dock.par_title_default", { name: sessionName(s.name), n: hubN })
    : t("web.dock.par_title_own", { name: sessionName(s.name) });
  return m;
}

// an own number that is not in the menu (e.g. "7 for this session" — also by click): the same confirmation dialog with a field
function askNumber(s, n, set) {
  const f = el("input", "input par-input");
  Object.assign(f, { type: "number", min: 0, max: OWN_MAX, step: 1, value: String(n), inputMode: "numeric" });
  f.setAttribute("aria-label", t("web.dock.par_number_aria"));
  numberDlg(t("web.dock.par_number_title", { name: sessionName(s.name) }), t("web.dock.par_number_text"),
    f, () => { const v = Number(f.value); if (/^\d+$/.test(f.value.trim()) && v <= OWN_MAX) set(v); else banner(t("web.dock.par_number_bad", { max: OWN_MAX })); });
}

// conversation size: tokens and the share of the compaction threshold (S.snap.context_limit); yellow from 75%, red from 90%
function contextMeter(s) {
  const n = Number(s.context) || 0, limit = Number(S.snap?.context_limit) || 0;
  if (!n || !limit || ["closed", "failed"].includes(s.state)) return null;
  const pct = Math.round(n / limit * 100);
  const m = el("span", "stat-ctx");
  m.append(el("span", "stat-ctx-tok", t("web.dock.ctx_tokens_lead", { tokens: kTok(n) })), `${pct}%`);   // narrow — percent only
  m.dataset.level = pct >= 90 ? "high" : pct >= 75 ? "warn" : "ok";
  m.title = pct >= 100 ? t("web.dock.ctx_title_over", { tokens: n.toLocaleString(locale), limit: limit.toLocaleString(locale) })
    : t("web.dock.ctx_title_under", { tokens: n.toLocaleString(locale), limit: limit.toLocaleString(locale) });
  return m;
}

// tokens in the tooltip, short: 5k, 1.3k, 842 (in the page language)
function kTok(n) {
  n = Number(n) || 0;
  if (n >= 10000) return t("web.dock.thousands", { n: Math.round(n / 1000) });
  if (n >= 1000) return t("web.dock.thousands", { n: Number((n / 1000).toFixed(1)).toLocaleString(locale) });
  return String(Math.round(n));
}

// a worker has handed in its task and sleeps: other senders' messages do not wake it, the owner's message in its field does
const dormant = (s) => isRunner(s) && s.state === "asleep" && s.retired;
const DORMANT = t("web.dock.dormant");
const STOPPED = t("web.dock.stopped");   // the owner stopped it ("Stop executor"): not "handed in its task"
const HINTS = {
  held: t("web.dock.hint_held"),
  dialog: t("web.dock.hint_dialog"),
  asleep: t("web.dock.hint_asleep"),
  busy: t("web.dock.hint_busy"),
};

// the target the last message went with, per session: the props are recreated on every snapshot, and a send failure
// arrives later — exactly that target has to be restored
const usedTarget = {};
// the board cards that went with a message (the send's own body → the cards): the failure of that very send puts them back
const riding = new WeakMap();

// the session input field's props (js/composer.js): recomputed on every snapshot from the fresh session, the field is not recreated
function composerProps(s) {
  const mine = () => (S.replyTarget && S.replyTarget.view === s.name ? S.replyTarget : null);   // the "Reply" target — in this feed
  const target = mine();
  const hub = s.kind === "hub";
  const direction = (S.snap.directions || []).includes(s.name);   // board cards picked on its board ride with its messages
  // a mini-hub's executor: the owner's words go to the mini-hub, which passes them on (the bus; core/reducer.py), a
  // reply in its own feed too; the wake of one that handed in (no reply target) and a reply to another session go
  // where they are addressed
  const relay = s.relay && (target ? target.session === s.name : !dormant(s));
  let reply = null;
  if (target) {
    const q = plainText(target.quote);   // the words, without the markdown (the quote sent keeps its source)
    const on = q ? t("web.dock.quoted", { text: `${q.slice(0, 80)}${q.length > 80 ? "…" : ""}` }) : "";
    const other = target.session !== s.name ? t("web.dock.reply_other", { session: sessionName(target.session) }) : "";
    // a quote is a card in the field (the quote itself, up to two lines); without one — the line as before
    reply = q ? { quote: q.length > 400 ? q.slice(0, 400) + "…" : q, title: other, cancel: () => { S.replyTarget = null; render(); } }
      : { text: other || t("web.dock.reply_here", { quote: on }), cancel: () => { S.replyTarget = null; render(); } };
  }
  // aimed at a question card: the bar over the field names the card and its marked option, "Cancel" takes the aim off
  const aimed = aimOf(s), stale = !aimed && staleAim(s);
  if (aimed) {
    const label = aimLabel(askKeys(s).find(([k]) => k === aimed)[1]), pick = picksOf(aimed);
    reply = { text: pick ? t("web.dock.aim_chip_pick", { label, pick }) : t("web.dock.aim_chip", { label }), cancel: () => aim(s, aimed) };
  } else if (stale) reply = { text: t("web.dock.aim_gone"), cancel: () => { S.cardAim = null; render(); } };
  return {
    // aimed at a card: the words go as its answer (cards.js __comment — the card collapses and delivers by itself);
    // files have no place in an answer — their paths follow the words
    send: aimed ? (body) => {
      const k = aimed, paths = (body.attachments || []).map((m) => m?.path).filter(Boolean);
      if ((body.attachments || []).length > paths.length) return { receipt: "refused", error: t("web.dock.aim_no_files") };
      const text = [body.text, ...paths].filter(Boolean).join("\n");
      // the core refused later: the words come back into the field (into the draft: a redraw takes the field from it)
      // with the reason; the field is aimed at the card again if it is open again (open) and the owner has pointed at
      // nothing since (another card, "Reply")
      const lost = (why, open) => {
        S.drafts[s.name] = text + (S.drafts[s.name] ? "\n" + S.drafts[s.name] : "");
        S.errors[s.name] = why;
        if (open && !S.cardAim && S.replyTarget?.view !== s.name) S.cardAim = { view: s.name, key: k };
        render();
      };
      // the card collapses and takes the aim off itself (__done, with a render); a refusal here leaves the aim on, with
      // the words back in the field
      if (!cardCache.get(k)?.__comment?.(text, lost)) return { receipt: "refused", error: t("web.dock.aim_gone") };
      return {};
    } : stale ? () => {
      S.cardAim = null;   // said once: the next Enter sends the words as a plain message
      render();
      return { receipt: "refused", error: t("web.dock.aim_gone") };
    } : null,
    holdLinks: true,   // a hub bus link in the words of an answer reaches the session too
    key: s.name, id: "msg", hotkey: true, to: hub ? t("web.dock.hub") : s.name, commands: s.name,
    label: hub ? t("web.dock.label_hub") : t("web.dock.label_session", { name: sessionName(s.name) }),
    placeholder: (aimed || stale ? t("web.dock.aim_ph") : target ? t("web.dock.reply_ph", { session: sessionName(target.session) }) : hub ? t("web.dock.hub_ph")
      : relay ? t("web.dock.relay_ph", { name: sessionName(s.relay) }) : t("web.dock.message_ph")) + spaceHint(),   // the session's name is in the header
    hint: aimed || stale ? "" : relay ? t(STOPPABLE.includes(s.state) ? "web.dock.relay_stop" : "web.dock.relay", { name: sessionName(s.relay) })
      : dormant(s) ? (s.stopped ? (s.stopped.by === "owner" ? STOPPED : t("web.dock.stopped_by", { name: sessionName(s.stopped.by) })) : DORMANT)
      : HINTS[s.state] || t("web.dock.hint_default"),
    reply,
    body: (text, files) => {
      if (aimed) { usedTarget[s.name] = null; return { session: s.name, text, attachments: files.map((p) => p.meta) }; }
      const t = usedTarget[s.name] = mine();
      const body = { session: t ? t.session : s.name, text, attachments: files.map((p) => p.meta) };
      if (direction && (!t || t.session === s.name) && pickedList(s.name).length) {   // also with a quote: it is the same session
        riding.set(body, pickedList(s.name));
        Object.assign(body, carry(s.name, text));
      }
      if (!t && dormant(s)) body.wake = true;   // a worker that handed in: the owner's message in its field wakes it
      if (t?.delegation) body.reply_to = t.delegation;
      if (t?.quote) body.quote = t.quote;
      return body;
    },
    // the cards leave with the message (the row of chips above the field empties) and come back if this very send fails
    before: (body) => { if (usedTarget[s.name]) S.replyTarget = null; if (riding.has(body)) clearPicks(s.name); render(); },
    failed: () => { if (usedTarget[s.name]) S.replyTarget = usedTarget[s.name]; render(); },
    restore: (body) => { if (riding.has(body)) restorePicks(s.name, riding.get(body)); },
    tray: direction ? pickTray(s.name) : null,
  };
}

function composer(s) {
  const c = buildComposer(composerProps(s));
  c.node.__composer = c;   // the snapshot updates it without recreating it (refreshTodosAndStatus)
  return c.node;
}
