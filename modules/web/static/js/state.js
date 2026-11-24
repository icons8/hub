// Shared page state and re-rendering. All modules read and change it here, not in their own copies.
import { t } from "./i18n.js";
export const S = {
  snap: null,          // core snapshot from /api/stream
  unread: [],          // plugins: (session, snapshot) → "unread" instead of the core (hub.addUnread)
  selected: null,      // session name from the address #/s/<name>
  cur: null,           // the session currently on screen
  cardAim: null,   // the session's field is aimed at a question card: {view — in which feed, key — the card} (dock.js)
  replyTarget: null,   // "Reply" to a message: {session — to whom, delegation?, quote — to what, view — in which feed}
  drafts: {},          // input field drafts per session
  errors: {},          // input field errors per session
  pending: {},         // attachments not sent yet: {key, name, size, meta, preview}
  expanded: new Set(), // expanded tool calls
  openSteps: new Set(), // expanded collapsed steps of a turn (key — seq of the first step)
  compact: true,        // "Brief": requests and replies are visible, intermediate steps are collapsed
  brief: {},            // what else is visible in "Brief" — checkboxes on the "View" tab
  shortPaths: false,    // file paths show only the name with extension, the full path is the link and the title
  feedAtEnd: false,     // a session with unread opens at the end of the feed, not at the "New" line
};

// "Brief" is quiet by default, like the former "Minimal"; checkboxes bring back
// everything that is in "Detailed" (set in the hub settings; two views — "Brief" and "Detailed")
export const BRIEF = [
  { key: "steps", on: false, label: t("web.brief.steps"), hint: t("web.brief.steps_hint") },
  { key: "count", on: false, label: t("web.brief.count"), hint: t("web.brief.count_hint") },
  { key: "big", on: false, label: t("web.brief.big"), hint: t("web.brief.big_hint") },
  { key: "path", on: false, adv: true, label: t("web.brief.path"), hint: t("web.brief.path_hint") },
  { key: "schedule", on: false, adv: true, label: t("web.brief.schedule"), hint: t("web.brief.schedule_hint") },
  // everything that is in "Detailed" can also be turned on in "Brief", so anyone can tune it flexibly
  { key: "open", on: false, adv: true, label: t("web.brief.open"), hint: t("web.brief.open_hint") },
  { key: "output", on: false, adv: true, label: t("web.brief.output"), hint: t("web.brief.output_hint") },
  { key: "groups", on: false, adv: true, label: t("web.brief.groups"), hint: t("web.brief.groups_hint") },
  { key: "meta", on: false, adv: true, label: t("web.brief.meta"), hint: t("web.brief.meta_hint") },
  { key: "task", on: false, adv: true, label: t("web.brief.task"), hint: t("web.brief.task_hint") },
  { key: "quote", on: false, adv: true, label: t("web.brief.quote"), hint: t("web.brief.quote_hint") },
  // hide a scheduled turn without a reply and the retelling of a reply to a card; the conversation line follows the
  // last message: on by default, turning one off restores the previous behavior (users can turn them back)
  { key: "idle", on: true, adv: true, label: t("web.brief.idle"), hint: t("web.brief.idle_hint") },
  { key: "relay", on: true, adv: true, label: t("web.brief.relay"), hint: t("web.brief.relay_hint") },
  { key: "last", on: true, adv: true, label: t("web.brief.last"), hint: t("web.brief.last_hint") },
  // a long message in a thread with another hub folds like a long message of a session (three lines and "more"): its own
  // checkbox, on by default; unchecked — the whole text
  { key: "hubfold", on: true, label: t("web.brief.hubfold"), hint: t("web.brief.hubfold_hint") },
  // the core's own 👀 (taken) and 👍 (answered) under the owner's messages; unchecked — hidden (a view choice: the core still
  // reports them); his own reactions of other emoji stay
  { key: "receipts", on: true, adv: true, label: t("web.brief.receipts"), hint: t("web.brief.receipts_hint") },
  // folding of answered items: off until checked, the "Reactions" module included — an answered reply the owner is still
  // reading neither folds nor grays when the next message goes
  { key: "resolved", on: false, label: t("web.brief.resolved"), hint: t("web.brief.resolved_hint") },
];
const BRIEF_KEY = "hub.brief";
const SHORT_PATHS_KEY = "hub.short_paths";
const FEED_AT_END_KEY = "hub.feed_at_end";
function readFeed() {
  let mode = "compact", brief = {};
  try {
    mode = localStorage.getItem("hub.feed") || "compact";
    brief = JSON.parse(localStorage.getItem(BRIEF_KEY) || "{}") || {};
    if (mode === "minimal") {   // there used to be a third view, "Minimal" — it is "Brief" without extras
      mode = "compact";
      brief = { ...brief, steps: false, count: false, big: false };
      localStorage.setItem("hub.feed", mode);
      localStorage.setItem(BRIEF_KEY, JSON.stringify(brief));
    }
    const sp = localStorage.getItem(SHORT_PATHS_KEY);
    if (sp !== null) S.shortPaths = sp === "1";
    S.feedAtEnd = localStorage.getItem(FEED_AT_END_KEY) === "1";
  } catch { /* no storage — defaults */ }
  S.compact = mode !== "full";
  S.brief = Object.fromEntries(BRIEF.map((o) => [o.key, typeof brief[o.key] === "boolean" ? brief[o.key] : o.on]));
}
readFeed();

// the "Fold answered" checkbox
export const foldOn = () => S.brief.resolved === true;

export function setCompact(on) {
  S.compact = on;
  try { localStorage.setItem("hub.feed", on ? "compact" : "full"); } catch { /* no storage — this time only */ }
}
export function setBrief(key, on) {
  S.brief = { ...S.brief, [key]: on };
  try { localStorage.setItem(BRIEF_KEY, JSON.stringify(S.brief)); } catch { /* no storage — this time only */ }
}
export function setShortPaths(on) {
  S.shortPaths = on;
  try { localStorage.setItem(SHORT_PATHS_KEY, on ? "1" : "0"); } catch { /* no storage — this time only */ }
}
export function setFeedAtEnd(on) {
  S.feedAtEnd = on;
  try { if (on) localStorage.setItem(FEED_AT_END_KEY, "1"); else localStorage.removeItem(FEED_AT_END_KEY); } catch { /* no storage — this time only */ }
}

// an executor for a task is the executor role without a backlog; a direction (raised via hub task run, same role but with
// a backlog) is not an executor: it is not cleaned up and does not take its raiser's place (core: core/minihub.runner;
// the hub's "clean up after executors" once proposed deleting 14 directions)
export const isRunner = (s) => s.role === "executor" && !s.backlog;

// the step a line inserted mid-turn waits behind: Claude Code reads stdin only between steps, so it is the oldest step of
// the session still running (its timeline: a tool row without a result; a background command gets its result at once)
// that had started by the time the line was typed (at); a step started later means a boundary was passed — no step
export function runningStep(name, at) {
  return (S.snap.timelines[name] || []).find((it) => it.kind === "tool" && it.status === "running" && it.ts <= at) || null;
}

// the core's state of the owner's message (queued, typing, aside, …); words passed to the mini-hub (it.relayed) are not this
// session's message — none
export const msgState = (it) => (it.kind === "in" && !it.relayed ? (S.snap.messages?.[it.id] || {}).state : undefined);
// the core holds the message and the session does not have it yet: the strip above the field shows it (queued, aside,
// "Sending"), the feed does not (main.js feedList)
export const notDelivered = (it) => ["queued", "typing", "aside"].includes(msgState(it));

// the process is not running (asleep, failed, closed) — for actions on the process: put to sleep, dismiss
export const RESTING = ["asleep", "failed", "closed"];
// resting in the list (collapsed group, muted row, "Remove from list") — core status: asleep or failed;
// asleep with a question is waiting for you, not here. An unknown or empty status counts as idle
// (EXTENDING: "Sessions in the snapshot")
export const resting = (s) => ["asleep", "failed"].includes(s.status);

// working executors of each session — {name: [executors]}, one pass over the snapshot: an executor's executor is counted
// for its executor parent and further up, until the first non-executor (the hub is not counted). An idle mini-hub
// whose task they carry looks like it is working (otherwise it would look as if they were just standing); the core
// does not change its state — the watchdog, parking and restart still see it as idle, as before
function crewAtWork(sessions) {
  const by = new Map(sessions.map((x) => [x.name, x])), out = {};
  for (const x of sessions) {
    if (!isRunner(x) || x.status !== "working") continue;
    let up = x.raised_by;
    for (let i = 0; up && i < 5; i++) {
      const p = by.get(up);
      if (!p || p.kind === "hub") break;
      const mine = (out[up] ||= []);
      if (!mine.includes(x.name)) mine.push(x.name);   // a raised_by cycle in an old journal — no repeats
      if (!isRunner(p)) break;
      up = p.raised_by;
    }
  }
  return out;
}
// the snapshot's session list is replaced by a new array (patch), so compute once per snapshot, not per row and header
const crews = new WeakMap();
// working executors of an idle session — for the "working" badge in the list and header and the line at the bottom of
// the feed; busy, asleep, or waiting-for-you sessions get an empty list: they have their own marker
export function crewOf(s) {
  if (s.status !== "idle" || s.state !== "idle") return [];
  const all = S.snap?.sessions || [];
  if (!crews.has(all)) crews.set(all, crewAtWork(all));
  return crews.get(all)[s.name] || [];
}
// label of the main session on the page — "Hub" and the hub name from settings: "Hub Orbit"; a name where
// "hub" is already a separate word in the page language or in English ("Personal Hub", "Orbit Hub") is shown as is
// ("GitHub mirror" becomes "Hub GitHub mirror"). The session name in the core does not change
export function hubLabel() {
  const word = t("web.sidebar.hub"), name = String(S.snap?.hub_name || "").trim();
  if (!name) return word;
  const words = name.toLowerCase().split(/[\s\p{P}]+/u);
  return words.includes(word.toLowerCase()) || words.includes("hub") ? name : t("web.sidebar.hub_named", { name });
}
// how a session is named on the page: the hub — by its title, the bus's liaison (a system session whose name is a
// Latin id, hub send liaison) — in the language of the page; the rest — as they are. The name itself never changes.
export function sessionLabel(s) {
  if (s.kind === "hub") return hubLabel();
  return sessionName(s.name);
}
// a session's name in a text of the page (a field's hint, a dialog, a feed line): the liaison — in the language of the
// page, the rest as they are. The same word as sessionLabel; for a place that has only the name
export function sessionName(name) {
  return name === "liaison" && (S.snap?.sessions || []).some((s) => s.name === name && s.system) ? t("web.sidebar.liaison") : name;
}
// note for the label: a mini-hub already writes "waiting for executors · N working" (core, core/minihub.waits_label),
// others get "executors: N"
export const crewNote = (s, n) => (n && !s.label_crew ? t("web.state.crew_note", { n }) : "");

let renderer = () => {};
export function onRender(fn) { renderer = fn; }
// a redraw for an action (a click, a page of the past arriving) used to put the whole feed in anew, and a selection in the
// feed went with it. The feed keeps its unchanged rows now (main.js mergeFeed): the selection would stay and hold the live
// snapshots back as long as it is there (renderLive) — a click on "More" or a step left the page standing still. It goes
// as it did
export function render() {
  if (!S.snap) return;
  // a text field has a selection of its own (a radio or a box in a card has not)
  const sel = document.getSelection(), field = !!document.activeElement?.matches?.("textarea, input:not([type=radio], [type=checkbox], [type=button], [type=submit])");
  if (sel && !field && !sel.isCollapsed && document.querySelector("main .feed")?.contains(sel.anchorNode)) sel.removeAllRanges();
  renderer();
}

// server updates wait while the owner is selecting text on the page: a re-render rebuilds the feed, and the
// selection used to vanish along with the old nodes (selecting text became almost impossible). The owner's own
// actions (click, expand a step) are drawn at once — via render(). Held for at most HOLD_MS: a forgotten selection
// must not freeze the page
const HOLD_MS = 120000;
let held = 0;
function selecting() {
  const a = document.activeElement;
  if (a && (a.tagName === "TEXTAREA" || a.tagName === "INPUT")) return false;   // an input field has its own selection
  const sel = document.getSelection();
  return !!sel && !sel.isCollapsed && sel.rangeCount > 0 && !!document.getElementById("app")?.contains(sel.anchorNode);
}
// and while the owner is scrolling the feed: a re-render replaces the feed with a new node and takes the position
// from the main thread one frame behind — a wheel step between events was rolled back, and scrolling up in a working
// session was impossible. We wait until scrolling settles (SCROLL_IDLE), but no longer than SCROLL_HOLD_MS. Scrolling
// set by the page itself (setScroll) is not counted — otherwise every re-render would postpone the next one
const SCROLL_IDLE = 300, SCROLL_HOLD_MS = 15000;
let scrolledAt = 0, scrollHeld = 0, scrollTimer = 0;
document.addEventListener("scroll", (e) => {
  const t = e.target;
  if (!t.classList?.contains("feed")) return;
  if (t.__set === t.scrollTop) { t.__set = null; return; }
  t.__own = true;   // the feed's own scroll listener (main.js follow) will sort it out: the owner took the scroll
  scrolledAt = Date.now();
}, true);
export function setScroll(f, top) { f.scrollTop = top; f.__set = f.scrollTop; }
function scrolling() { return Date.now() - scrolledAt < SCROLL_IDLE; }
export function ownerScrolled() { return scrolledAt > 0; }
// and while a mouse button or a finger is down: a redraw between press and release takes the pressed node (a link, or
// the `code` / `b` inside it) out of the page, and the browser sends no click to a removed node — "a link needs several
// clicks". Every redraw that is not an action of the owner goes through renderLive, so one hold covers the feed, the
// side panels, the plugins' slots, the refresh of a direction board and the page of earlier history alike. A press is short: we wait for the release (the redraw follows it, after the
// click), but no longer than PRESS_HOLD_MS — a release the page never saw (outside the window, a context menu) must not
// freeze the page
const PRESS_HOLD_MS = 3000, AFTER_RELEASE_MS = 100;
let pressedAt = 0, releasedAt = 0, pressHeld = false, pressTimer = 0;
// the hold lasts while the button is down and AFTER_RELEASE_MS after the release: the click is a separate event after
// pointerup (on touch a tap comes later still), and a snapshot that arrives in that gap must not redraw before it
const holdEnd = () => pressedAt > 0 ? pressedAt + PRESS_HOLD_MS : releasedAt + AFTER_RELEASE_MS;
const pressing = () => Date.now() < holdEnd();
function released() {
  if (pressedAt > 0) releasedAt = Date.now();
  pressedAt = 0;
  if (!pressHeld) return;
  clearTimeout(pressTimer);
  pressTimer = setTimeout(renderLive, AFTER_RELEASE_MS + 10);   // not at once: the click of this release is still to be delivered
}
document.addEventListener("pointerdown", () => { pressedAt = Date.now(); }, true);
document.addEventListener("pointerup", released, true);
document.addEventListener("pointercancel", released, true);
addEventListener("blur", released);
export function renderLive() {
  if (!S.snap) return;
  if (pressing()) {
    pressHeld = true;
    clearTimeout(pressTimer);
    pressTimer = setTimeout(renderLive, holdEnd() - Date.now() + 10);   // the release moves this up; if it never comes — redraw anyway
    return;
  }
  clearTimeout(pressTimer);
  pressTimer = 0;
  pressHeld = false;
  if (selecting() && (!held || Date.now() - held < HOLD_MS)) { held ||= Date.now(); return; }
  if (scrolling() && (!scrollHeld || Date.now() - scrollHeld < SCROLL_HOLD_MS)) {
    scrollHeld ||= Date.now();
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(renderLive, SCROLL_IDLE);
    return;
  }
  held = 0;
  scrollHeld = 0;
  renderer();
}
document.addEventListener("selectionchange", () => { if (held && !selecting()) renderLive(); });
