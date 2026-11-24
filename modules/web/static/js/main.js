// Entry point: the address, session rendering (header, feed, bottom), keys, the spinner and the turn stopwatch.
import { $, el, icon, elapsed, SPIN, ago, avatarNode, dayTime, lookSig, placeNodes, stepText, unticked, OWN } from "./dom.js";
import { S, isRunner, resting, onRender, render, renderLive, setCompact, setScroll, ownerScrolled, crewOf, crewNote, notDelivered, sessionLabel, sessionName } from "./state.js";
import { banner, connect, post } from "./api.js";
import { adopt, foldTall, item as feedItem, items as feedItems, jump, keyOf, textBlock, textsIn } from "./feed.js";
import { quoteSource } from "./quotefind.js";
import { isOpen as searching, open as openSearch, painted, setup as setupSearch, toggle as toggleSearch, trimmed as searchTrimmed } from "./feedsearch.js";
import { dock, dockKey, ownerCards, refreshTodosAndStatus } from "./dock.js";
import { dismissAsk } from "./cards.js";
import { askTrain, interrupt, lifeItems, STOPPABLE, stopExecutor, stoppedExecutor, stopping, stoppingExecutor } from "./dialogs.js";
import { awayTitle, bindSearch, renderList, shortName } from "./sidebar.js";
import { bindAttachments } from "./attachments.js";
import { scheduleView, inTime, jobOf } from "./schedule.js";
import { settingsView } from "./settings.js";
import { hubView, busNews } from "./hubs.js";
import { jumpDown } from "./jumpdown.js";
import { boardChoices, boardFrame, boardOpen, boardSwitch, markOpen } from "./boards.js";
import { extView, extToolbar, extHide, extLoading, focusSearch, loadExtensions, registerBuiltIn, routeOf, slotNodes } from "./ext.js";
import { applySavedTheme, reconcileSavedTheme, themeFromLink } from "./themes.js";
import { applySavedTextScale } from "./textscale.js";
import { updatesView } from "./updates.js";
import { failed, loaded, nextTry, olderKnown, retry, sync } from "./timeline.js";
import { waiting as waitingOut } from "./outgoing.js";
import { showWait } from "./backoff.js";
import { moreMenu } from "./menu.js";
import { modelTitle } from "./modelpicker.js";
import { t, dir as pageDir } from "./i18n.js";
import { openPalette } from "./groups.js";
import { bindModuleViews } from "./modview.js";
import { mcpBar, renderMcp } from "./mcpwait.js";
import { kitView } from "./kit.js";
import { minBar, renderMin } from "./minbar.js";
import { setupBar, renderSetup } from "./setup.js";
import "./quotesel.js";   // the panel by a selection in the feed: reply, reaction, copy
import "./msgkeys.js";   // ⌘↑/⌘↓ reply, Esc drops the quote, typing outside the field: ahead of the Esc below
import registerClean from "../clean/clean.js";   // the Clean theme's page code: acts only under html.th-clean

const readHash = () => {
  themeFromLink();
  // schedule: everything is #/schedule, one session's is #/schedule/<session> (the URL says where you are)
  const sch = location.hash.match(/^#\/schedule(?:\/(.+))?$/);
  if (sch) { S.scheduleFor = sch[1] ? decodeURIComponent(sch[1]) : null; return ":schedule"; }   // a session name never contains ":"
  const kit = location.hash.match(/^#\/kit\/(.+)$/);   // "Skills and MCP" of a session (kit.js)
  if (kit) { S.kitFor = decodeURIComponent(kit[1]); return ":kit"; }
  if (location.hash === "#/flow") return ":x:flow";   // Flow is a preinstalled plugin (bundled/flow), the old address
  if (location.hash === "#/updates") return ":updates";
  if (location.hash === "#/sessions") return ":sessions";   // phone: the session list sits behind the "All sessions" button; on a wide screen — the hub feed
  const x = routeOf(); if (x) return ":x:" + x.id;   // a plugin view; since 1.28 also with the item's params (#/x/<id>?k=v)
  if (location.hash === "#/settings" || location.hash === "#/browsers") return ":settings";
  const hb = location.hash.match(/^#\/hub\/(.+)$/);   // a linked hub: the owners' chat (hubs.js)
  if (hb) return ":hub:" + decodeURIComponent(hb[1]);
  const bd = location.hash.match(/^#\/board\/(.+)$/);   // a direction board (hub direction board <slug>)
  if (bd) { markOpen(decodeURIComponent(bd[1])); return decodeURIComponent(bd[1]); }
  const m = location.hash.match(/^#\/s\/(.+)$/); return m ? decodeURIComponent(m[1]) : null;
};

// what has already been asked from the board: the header is redrawn on every snapshot, and the "Asked" label on a button
// would be lost
const recapAsked = new Map();   // "session|button" → {text, failed} after a press; an error can be retried
const resuming = new Set();   // sessions whose "Resume" request is on its way
// The "Board" of a direction session (the list shows just a task counter, and inside is a dedicated direction recap):
// how many are open, what is in progress, what it is waiting for; if silent — ask it; if there are no tasks — finishing is up to you
function directionRecap(s, dir) {
  const box = el("div", "dir-recap");
  const parts = [dir.open ? t("web.main.dir_open", { count: dir.open }) : t("web.main.dir_none")];
  // core facts (core/board.py): how many cards have executors at work, how many were handed in and not yet sorted —
  // placed before the "in progress" list, which the line truncates
  if (dir.working) parts.push(t("web.main.dir_working", { count: dir.working }));
  if (dir.unsorted) parts.push(t("web.main.dir_unsorted", { count: dir.unsorted }));
  if (dir.doing.length) parts.push(t("web.main.dir_doing", { list: dir.doing.join("; ") }));
  box.append(el("span", "dir-board", t("web.main.dir_board", { parts: parts.join(" · ") })));
  const ask = (label, to, text, done) => {
    const key = `${s.name}|${label}`, was = recapAsked.get(key);
    const b = el("button", "btn quiet small", was?.text || label);
    b.type = "button";
    b.disabled = !!was && !was.failed;
    b.addEventListener("click", async () => {
      b.disabled = true;
      recapAsked.set(key, { text: label });
      const r = await post("/api/send", { session: to, text, action: true });   // a button is not an answer to a card
      recapAsked.set(key, r.error ? { text: t("web.main.recap_failed", { error: r.error }), failed: true } : { text: done });
      render();
    });
    return b;
  };
  const busy = ["busy", "starting"].includes(s.state);
  if (s.waiting) box.append(el("span", "dir-wait", t("web.main.dir_waits", { text: s.waiting.text, ago: ago(s.waiting.ts) })));
  // it spoke, but was written to afterwards — its word stays visible, and asking again is the same button
  else if (dir.open && !busy) box.append(el("span", "dir-wait muted", s.waited
    ? t("web.main.dir_waited", { text: s.waited.text, at: dayTime(s.waited.stale) }) : t("web.main.dir_silent")),
    ask(t("web.main.ask_waiting"), s.name, t("web.main.ask_waiting_prompt"), t("web.main.ask_waiting_done")));
  else if (!dir.open && !busy) {
    const hub = (S.snap.sessions || []).find((x) => x.kind === "hub");
    box.append(el("span", "dir-wait", t("web.main.dir_finish_yours")));
    if (hub) box.append(ask(t("web.main.finish_direction"), hub.name, t("web.main.finish_direction_prompt", { name: s.name }),
      t("web.main.finish_direction_done")));
  }
  return box;
}

const HEAD_CHIPS = 2;   // no more than two chips in the header
function head(s) {
  const h = el("div", "head");
  const back = el("button", "btn quiet back", t("web.nav.all_sessions"));
  back.type = "button";
  back.addEventListener("click", () => { location.hash = "#/sessions"; });
  // idle while its executors work — shown in the "working" color; a mini-hub already writes "waiting for executors · N" (core)
  const crewN = crewOf(s).length;
  const pill = el("span", "pill", (s.label || "") + crewNote(s, crewN));
  pill.dataset.s = crewN ? "busy" : s.state;
  if (s.stopped && s.state === "asleep") {   // stopped at once (owner, hub): not "handed in", and not an error
    pill.textContent = s.stopped.by === "owner" ? t("web.main.stopped_you", { at: dayTime(s.stopped.ts) })
      : t("web.main.stopped_by", { name: sessionName(s.stopped.by), at: dayTime(s.stopped.ts) });
    pill.dataset.s = "stopped";
  }
  if (s.compacting && ["idle", "busy"].includes(s.state)) { pill.textContent = t("web.sidebar.compacting"); pill.dataset.s = "busy"; }   // not "idle": its conversation is being compacted
  const title = el("h2", null, sessionLabel(s));
  title.title = s.name;
  // the session icon (its own or the previous one) sits next to the name; a click opens the palette (js/groups.js)
  const ic = s.icon;
  if (ic) {
    const av = avatarNode(ic, "avatar head-av");
    av.title = t("web.groups.change_icon");
    av.addEventListener("click", () => openPalette(av.getBoundingClientRect(), s.name));
    title.prepend(av, document.createTextNode(" "));
  }
  // plugin nodes before the name (1.35): after "back" (a narrow screen) and before the title in the markup, so Tab
  // follows the visible order
  h.append(back, ...slotNodes("session.header.start", { session: s }), title, pill);
  // away: the list with its strip is a screen away on a phone, so the mark is in the header too (base.css hides it where the list is beside)
  const away = S.snap.owner || {};
  if (away.state === "away") h.append(Object.assign(el("span", "chip away warn", t("web.main.away_chip")), { title: awayTitle(away) }));
  // what it is working on right now is shown only in the feed next to "Working" and in the list on the left, not duplicated in the header
  // no more than two chips in the header (now only the schedule one); the rest and the tools go into the "More" menu
  const chips = [];   // {node, item}: the node for the header and the same chip as a menu item
  // Opus's safety filter moved it to another model (core/refusals.py): what happened and what the core does — first, it
  // is about the session's own model; a server without the field draws no chip
  const rf = s.refused;
  // "conversation cleaned" only when the core cut the stopped turn out (cut), otherwise the model alone came back
  const said = rf && { waits: "web.main.refused_waits", again: "web.main.refused_again",
    back: rf.cut === false ? "web.main.refused_back_kept" : "web.main.refused_back" }[rf.state];
  if (said) {
    const names = { asked: modelTitle(rf.asked), got: modelTitle(rf.got) };
    const text = t(said, names) + (rf.day > 1 ? t("web.main.refused_day", { count: rf.day }) : "");
    // a repeat waits for the owner's hand: the chip is that hand — the same restart as `hub session reload`
    const again = rf.state === "again";
    const c = el(again ? "button" : "span", "chip refused" + (rf.state === "back" && rf.day < 2 ? "" : " warn"), text);
    c.title = t("web.main.refused_title", { ...names, at: dayTime(rf.ts) })
      + (again ? "\n" + t("web.main.refused_return", names) : "");
    const back = async () => {
      const r = await post("/api/session/reload", { session: s.name });
      if (r.error) banner(r.error);
    };
    if (again) { c.type = "button"; c.addEventListener("click", back); }
    chips.push({ node: c, item: again ? { label: text, title: c.title, onClick: back } : { text, title: c.title } });
  }
  // its jobs in the schedule (on entering a session you can see whether it has tasks in the schedule)
  const jobs = (S.snap.schedule || []).filter((j) => jobOf(j) === s.name && ["active", "paused"].includes(j.state));
  if (jobs.length) {
    const tasks = new Set(jobs.map((j) => j.group || j.id)).size;
    const next = Math.min(...jobs.map((j) => j.next || Infinity));
    const text = t("web.main.sched_tasks", { count: tasks }) + (Number.isFinite(next) ? ` · ${inTime(next)}` : "");
    const c = el("a", "chip sched" + (jobs.some((j) => j.state === "paused") ? " warn" : ""));
    // ⏰ as the theme's image if it has one (avatars: the page's glyphs are treated like session emoji)
    c.append(avatarNode("⏰", "chip-glyph"), document.createTextNode(` ${text}`));
    c.href = "#/schedule/" + encodeURIComponent(s.name);   // from a session — its schedule
    c.title = jobs.map((j) => `${j.when}: ${(j.prompt || j.command || "").replace(/\s+/g, " ").slice(0, 90)}`).join("\n");
    chips.push({ node: c, item: { label: text, title: c.title, onClick: () => { location.hash = c.getAttribute("href"); } } });
  }
  // what the session loaded and used (#/kit/<session>, kit.js) — in "More"; what the core granted is shown there too
  const tools = [{ item: { label: t("web.kit.open"), title: t("web.kit.open_hint"),
    onClick: () => { location.hash = "#/kit/" + encodeURIComponent(s.name); } } }];
  const shown = chips.slice(0, HEAD_CHIPS), more = [...chips.slice(HEAD_CHIPS), ...tools];
  if (shown.length) { const box = el("span", "chips"); box.append(...shown.map((x) => x.node)); h.append(box); }
  h.append(el("span", "spacer"));
  if (!((S.snap.directions || []).includes(s.name) && boardOpen(s.name))) {   // feed search (⌘F); a board has no feed
    const lupa = el("button", "btn quiet fs-lupa" + (searching(s.name) ? " on" : ""));
    lupa.type = "button";
    lupa.title = t("web.search.open");
    lupa.setAttribute("aria-label", t("web.search.open"));
    lupa.setAttribute("aria-pressed", String(searching(s.name)));
    lupa.append(icon("search"));
    lupa.addEventListener("click", () => toggleSearch(s.name));
    h.append(lupa);
  }
  if ((S.snap.directions || []).includes(s.name)) h.append(boardSwitch(s.name));  // a direction session: feed or board
  const seg = el("div", "seg fold-seg");   // fold-seg: a phone moves the switch into the "More" menu (base.css)
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", t("web.main.view"));
  for (const [on, label, title] of [[true, t("web.main.view_brief"), t("web.main.view_brief_tip")],
    [false, t("web.main.view_full"), t("web.main.view_full_hint")]]) {
    const b = el("button", null, label);
    b.type = "button";
    b.title = title;
    b.setAttribute("aria-pressed", String(S.compact === on));
    b.addEventListener("click", () => { setCompact(on); render(); });
    seg.append(b);
  }
  h.append(seg);
  const btn = (cls, label, title, onClick, iconName) => {
    const b = el("button", "btn " + cls);
    b.type = "button";
    if (iconName) b.append(icon(iconName));
    b.append(document.createTextNode(label));
    b.title = title;
    b.addEventListener("click", onClick);
    h.append(b);
  };
  const runner = isRunner(s);
  if (["busy", "dialog"].includes(s.state)) {
    const asked = Date.now() - (stopping.get(s.name) || 0) < 15000;
    // an executor has two stops: this one breaks only the turn (Esc), so it is named so next to "Stop executor"
    btn("", asked ? t("web.main.stopping") : runner ? t("web.main.stop_turn") : t("web.main.stop"), t("web.main.stop_hint"),
      () => { interrupt(s); render(); }, "stop");
    if (asked) h.lastChild.disabled = true;
  } else stopping.delete(s.name);
  if (runner && STOPPABLE.includes(s.state)) {
    // stop the executor now (the core kills its process tree, even mid-step); it wakes by the owner's message
    const asked = stoppingExecutor(s);
    btn("danger stop-exec", asked ? t("web.main.stopping") : t("web.main.stop_executor"), t("web.main.stop_executor_hint"),
      () => { stopExecutor(s); render(); }, "stop");
    if (asked) h.lastChild.disabled = true;
  } else stoppedExecutor(s);   // asleep: a woken one gets a working button at once
  // dangerous actions go into the "More" menu, not next to "Stop" with the same weight; chips that did not fit go there too
  const menu = [...more.map((x) => x.item)];
  // its turn was cut by a limit (core limit_cut): "Resume" continues it now, before the planned time; while the window is
  // still closed the core says so (409) and the session keeps waiting. A server without "session.resume" in its features
  // (older than API 1.36) has no such endpoint: no item. A second press while the first one is on its way posts nothing
  if (s.limit_cut && (S.snap.features || []).includes("session.resume")) menu.unshift({ label: t("web.main.resume"), title: t("web.main.resume_hint"), onClick: async () => {
    if (resuming.has(s.name)) return;
    resuming.add(s.name);
    try {
      const r = await post("/api/session/resume", { session: s.name });
      if (r.error) banner(r.error);
    } finally { resuming.delete(s.name); }
  } });
  // a system agent always exists: it cannot be fired, put to sleep or deleted
  const fixed = s.kind === "hub" || s.system;
  if (menu.length && (!fixed || s.trains)) menu.push({ sep: true });
  // the agent-training session (s.trains): review the fleet now and force-retrain the agents
  if (s.trains) menu.push({ label: t("web.main.train"), title: t("web.main.train_hint"), onClick: () => askTrain(s.name) });
  // agent-dev: the stops audit of the agents (core/system.stops_view) — a line, red when it does not run or is skipped
  if (s.stops) menu.push({ text: stopsLine(s.stops), cls: s.stops.alarm ? "bad" : "" });
  // the session has executors — an ordinary prompt to the hub: clean up after them
  const hubS = (S.snap.sessions || []).find((x) => x.kind === "hub");
  const execs = (S.snap.sessions || []).filter((x) => isRunner(x) && x.raised_by === s.name).map((x) => x.name);
  if (hubS && execs.length) menu.push({ label: t("web.main.cleanup"), title: t("web.main.cleanup_hint", { list: execs.join(", ") }),
    onClick: async () => {
      const r = await post("/api/send", { session: hubS.name, text: t("web.main.cleanup_prompt", { name: s.name, list: execs.join(", ") }), action: true });
      if (r.error) banner(r.error);
    } });
  // the number of executors and "on its own" live in the line under the input field (dock.js parallelChip)
  menu.push(...lifeItems(s));
  // "Brief | Detailed" and "Feed | Board" are in the menu too: on a phone the switches leave the header for it (CSS shows these
  // items there and hides them elsewhere); with nothing else in the menu the button itself is only for the phone
  const views = [[t("web.main.view_brief"), t("web.main.view_brief_tip"), true], [t("web.main.view_full"), t("web.main.view_full_hint"), false]]
    .map(([label, title, on]) => ({ label, title, checked: S.compact === on, cls: "head-fold head-fold-view", onClick: () => { setCompact(on); render(); } }));
  const openTasks = (S.snap.direction_tasks || {})[s.name]?.open || 0;
  const boards = (S.snap.directions || []).includes(s.name) ? boardChoices(s.name)
    .map((c) => ({ label: c.board && openTasks ? `${c.label} ${openTasks}` : c.label, title: c.title, checked: c.on, cls: "head-fold head-fold-board", onClick: c.pick })) : [];
  const fold = [...(boards.length ? [...boards, { sep: true, cls: "head-fold head-fold-board" }] : []), ...views,
    ...(menu.length ? [{ sep: true, cls: "head-fold head-fold-view" }] : [])];
  const moreWrap = moreMenu("head:" + s.name, [...fold, ...menu]);
  if (!menu.length) moreWrap.classList.add("head-fold-only");
  h.append(moreWrap);
  if (s.stops?.alarm) h.lastChild.querySelector(".menu-btn")?.classList.add("alert");   // seen without opening the menu
  // the right panel (the session.aside slot) is closed — an open button; an open one is closed by its ×
  if (sideNow.title && sideNow.name === s.name && !sideNow.open) {
    const b = el("button", "btn quiet side-toggle");
    b.type = "button";
    b.append(icon("board"), document.createTextNode(sideNow.title));
    b.title = t("web.main.side_show");
    b.addEventListener("click", () => setSide(s.name, true));
    h.append(b);
  }
  h.append(...slotNodes("session.header", { session: s }));   // plugin slots in the session header
  // the direction board and background work go in one thin line under the row with the name and buttons (so they do
  // not take much room at the top); what does not fit goes into the line's tooltip
  const meta = el("div", "head-meta"), tip = [];
  const dir = (S.snap.direction_tasks || {})[s.name];
  if (dir) { const r = directionRecap(s, dir); meta.append(r); tip.push(r.textContent); }
  // what runs in the background: how many, and what printed last (so it is clear what a session is doing)
  const bgs = [...(s.background || [])].sort((a, c) => a.since - c.since);
  if (bgs.length) {
    const line = el("span", "bg-line"), what = (b) => { const p = { title: b.title || b.id, elapsed: elapsed(b.since) }; return b.kind === "agent" ? t("web.main.bg_agent", p) : t("web.main.bg_command", p); };
    line.append(el("b", null, (bgs.length > 1 ? t("web.main.bg_many", { count: bgs.length }) : t("web.main.bg_one")) + " "), document.createTextNode(bgs.map(what).join("; ")));
    const one = bgs.length === 1 ? bgs[0] : null;
    if (one?.out_at) line.append(el("span", "muted", ` · ${t("web.main.bg_output", { ago: ago(one.out_at) })} `), el("code", null, one.out || "—"));
    if (dir) meta.append(el("span", "sep", "·"));
    meta.append(line);
    tip.push(...bgs.map((b) => `${t("web.main.bg_one")} ${what(b)}${b.out_at ? ` · ${t("web.main.bg_output", { ago: ago(b.out_at) })} ${b.out || "—"}` : ""}`));
  }
  // will become a mini-hub after the current task (core/minihub.py) — until then it works the old way, on its own
  if (s.minihub_pending) {
    if (meta.childNodes.length) meta.append(el("span", "sep", "·"));
    const line = el("span", "muted", s.minihub_pending[0].toUpperCase() + s.minihub_pending.slice(1));
    meta.append(line);
    tip.push(t("web.main.pending_general_tip", { text: line.textContent }));
  }
  // a mini-hub with uncommitted changes in its folder: executors do not see them — it hands the sorting to them (core/minihub.py)
  if (s.minihub_dirty) {
    if (meta.childNodes.length) meta.append(el("span", "sep", "·"));
    const line = el("span", "muted", s.minihub_dirty[0].toUpperCase() + s.minihub_dirty.slice(1));
    meta.append(line);
    tip.push(t("web.main.dirty_tip", { text: line.textContent }));
  }
  if (meta.childNodes.length) { meta.title = tip.join("\n"); h.append(meta); }
  return h;
}

// the stops audit line of the agent-dev menu: days since the last marked audit, a requested one not marked, or the alarm
function stopsLine(a) {
  const days = (ts) => Math.max(0, Math.floor((Date.now() / 1000 - (ts || 0)) / 86400));
  if (a.alarm === "off") return t("web.main.stops_off");
  if (a.alarm === "silent") return t("web.main.stops_silent", { count: days(a.ran) });
  if (a.alarm === "failed") return t("web.main.stops_failed", { code: String(a.check) });
  if (a.alarm === "skipped") return t("web.main.stops_skipped", { count: days(a.asked) });
  const last = a.done ? t("web.main.stops_last", { count: days(a.done) }) : t("web.main.stops_never");
  return a.asked ? last + " · " + t("web.main.stops_asked", { count: days(a.asked) }) : last;
}

function empty(title, text) {
  const e = el("div", "feed"), inner = el("div", "empty");
  inner.append(el("h2", null, title), el("p", null, text));
  e.append(inner);
  return e;
}

// the feed's past on scrolling up (journal records are loaded from the archive as you scroll): older than the live
// tail — from the core's feed, then from the archived journals (/api/history)
const older = {};   // session -> {items, more, loading, building, seek}
const sought = new Set();   // sessions whose first window was already checked for emptiness: the search itself runs only on entry
const olderAt = (name) => older[name] || (older[name] = { items: [], more: true, loading: false });
function olderOf(name, live) {
  const o = older[name];
  const first = live.length ? live[0].seq : Infinity;
  return o ? o.items.filter((it) => it.seq < first) : [];
}
// what happened (for the jump by quote): ok — a page arrived, building — the archive is still being parsed, busy — a
// request is already in flight, end — there is nothing earlier, fail — network, stale — the feed was trimmed while the
// request was running
let anchorFor = null;   // the session whose feed has just received older rows: renderMain0 keeps the reader's place in it
async function loadOlder(name) {
  const o = olderAt(name);
  if (o.loading || !o.more) return o.loading ? "busy" : "end";
  o.loading = true;
  const live = S.snap.timelines[name] || [];
  const shown = olderOf(name, live).concat(live);
  const before = shown.length ? shown[0].seq : 1e12;
  let r = null;
  try { r = await (await fetch(`/api/history?session=${encodeURIComponent(name)}&before=${before}&n=100`)).json(); }
  catch { /* network — we will try again on the next scroll */ }
  o.loading = false;
  if (older[name] !== o) return "stale";   // the feed was trimmed (S.trimmed) while the request ran — this past no longer joins up
  if (!r || !Array.isArray(r.items)) {
    if (o.seek) { o.seek = null; renderLive(); }   // the text search stalled — the button is back in the owner's hands
    return "fail";
  }
  if (o.seek && !r.building) o.seek.pages += 1;
  const have = new Set(o.items.map(keyOf));
  o.items = r.items.filter((it) => !have.has(keyOf(it))).concat(o.items);
  o.more = r.more;
  o.building = r.building;
  // the archive is still being parsed — retry; if the owner left the session there is no retry and the flag is cleared: on return seekStep continues the search
  if (r.building) setTimeout(() => { if (S.cur?.name === name) loadOlder(name); else o.building = false; }, 3000);
  // stay at the same place: the distance to the bottom is taken by renderMain0 at the moment of drawing, not here — the
  // redraw may wait for a held mouse button and the owner may scroll meanwhile
  if (S.cur?.name === name) anchorFor = name;
  renderLive();
  return r.building ? "building" : "ok";
}

// "Show earlier" searches until it finds text: page by page, until a block with text appears above the previous top —
// the owner's or the session's words, a question, an answer, whatever the current feed view shows ("Brief" hides steps
// and session correspondence in a steps row) — or the history ends. If a session is opened and the window has no text
// (steps only), the same search starts by itself. The limit is SEEK_PAGES pages of 100 rows: in a week-long journal
// the longest turn without text is 444 rows (decisions.md)
const SEEK_PAGES = 10;
// had — the keys of feed rows at the start of the search: the target is a new block with text above the first of them
// (by position in the feed, not by number: a message sits at its delivery time)
function seek(name, nodes) {
  const o = olderAt(name);
  if (!o.seek && o.more) o.seek = { had: new Set(nodes.map((n) => n.dataset?.key).filter(Boolean)), pages: 0 };
}
// the search step runs over the finished feed on each render: text found above the previous top, the history ended or the limit reached — stop
function seekStep(name, nodes) {
  const o = older[name];
  if (!o?.seek) return;
  const had = o.seek.had, top = nodes.findIndex((n) => had.has(n.dataset?.key));
  const found = nodes.slice(0, top < 0 ? nodes.length : top).some((n) => textBlock(n) && !had.has(n.dataset.key));
  if (found || !o.more || o.seek.pages >= SEEK_PAGES) o.seek = null;
  else if (!o.loading && !o.building) loadOlderSoon(name);
}
// load the past from the next tick, not from inside building the feed; still — is it still needed, done — what to do with the outcome
function loadOlderSoon(name, still, done) {
  setTimeout(() => { if (S.cur?.name === name && (!still || still())) loadOlder(name).then(done); }, 0);
}

// a jump by the "in reply to …" quote: the nearest session row before the message that contains the quote — in the
// loaded feed, otherwise in the past (/api/history, like "Show earlier", with the same page limit). If found, the feed
// lets go of the bottom, the row is centered in the window and highlighted; if not, the quote gets the reason
// (feed.js, jump.note). A new press cancels the previous search, and so does leaving the session. false — there is no
// feed: the quote simply expands, as before
const FLASH_MS = 1600, WAITS = 60, ANSWER_NOTE_MS = 6000;
const noteClocks = new Map();   // "answer:<id>" → the timer that takes its verdict down
let jumps = 0;
const hasOlder = (name) => (older[name] ? older[name].more : olderKnown(name) !== false);
const nap = (ms) => new Promise((ok) => setTimeout(ok, ms));
S.jumpQuote = (msg) => !!msg.quote && seekRow(msg, (rows) => quoteSource(rows, msg, textsIn));
// a press on the core's 👍 under the owner's message: to the row that answered it (reactions.links: its event number),
// the same way; the search mark is its own (not the message's quote): feed.js draws it under the message (answerNote)
S.jumpAnswer = (msg, seq) => seekRow({ id: "answer:" + msg.id }, (rows) => rows.find((x) => x.kind === "out" && x.seq === seq));
function seekRow(msg, pick) {
  const name = S.cur?.name;
  if (!name) return false;
  const mine = ++jumps;
  for (const [id, v] of jump.note) if (v === "seek") jump.note.delete(id);   // the previous search is cancelled — clear its "searching"
  const alive = () => mine === jumps && S.cur?.name === name;
  const find = () => {
    const live = S.snap.timelines[name] || [];
    return pick(olderOf(name, live).concat(live));
  };
  const found = find();
  if (found || !hasOlder(name)) { landOn(name, msg, found, "none"); return true; }
  stopClock(msg.id);   // a verdict's clock still running from the last press: this press starts over
  jump.note.set(msg.id, "seek");
  render();
  (async () => {
    let pages = 0, waits = 0, why = "none", x = null;
    while (alive()) {
      const st = await loadOlder(name);
      if (!alive() || (x = find())) break;
      if (st === "end" || st === "fail") { why = st === "end" ? "none" : "fail"; break; }
      if (st === "ok" && ++pages >= SEEK_PAGES) { why = "limit"; break; }
      if (st !== "ok") {   // the archive is being parsed, another request or a trim — wait and retry
        if (++waits > WAITS) { why = "fail"; break; }
        await nap(st === "building" ? 3000 : 250);
      }
    }
    if (alive()) landOn(name, msg, x, why);
    else if (mine === jumps) jump.note.delete(msg.id);   // left the session; if a new press cancelled it, that press already cleared "searching"
  })();
  return true;
}
const stopClock = (id) => { clearTimeout(noteClocks.get(id)); noteClocks.delete(id); };
function landOn(name, msg, x, why) {
  stopClock(msg.id);
  if (!x) {
    jump.note.set(msg.id, why);
    render();
    // the verdict under the 👍 goes away by itself (a quote's stays with the quote it opened): the message has nothing
    // of its own to hold it, and the next press on the 👍 says it again — for the full time: one clock per mark, a new
    // press winds it up anew
    if (String(msg.id).startsWith("answer:")) {
      noteClocks.set(msg.id, setTimeout(() => { noteClocks.delete(msg.id); if (jump.note.get(msg.id) === why) { jump.note.delete(msg.id); renderLive(); } }, ANSWER_NOTE_MS));
    }
    return;
  }
  jump.note.delete(msg.id);
  showRow(name, x);
}
// to row x of the session feed: expand it (turn steps, long text, a step with output), let go of the bottom, center it in the window, highlight it
function showRow(name, x) {
  const key = keyOf(x);
  if (x.kind === "tool" && x.id) S.expanded.add(x.id);   // a search match can be inside a step's output
  jump.reveal = new Set([key]);   // a row inside a turn's steps — its steps row is in view (the last one jumped to)
  jump.open = new Set([key]);     // and expanded — once, in this redraw (feed.js)
  S.openSteps.add("m" + (x.id ?? x.seq));   // a long one, collapsed to a few lines, is shown in full
  jump.flash = key;
  jump.until = Date.now() + FLASH_MS;
  const was = away[name];
  away[name] = true;   // as if the owner had scrolled up: the feed does not pull down
  const held = newMark[name]?.hold;
  release(name);       // and the "Last read" line does not hold: what was found is in the middle of the window
  holding[name] = false;
  const old = document.querySelector("main .feed");
  if (old) old.__pinned = false;
  render();
  jump.open.clear();
  const f = document.querySelector("main .feed");
  const n = f && [...f.querySelectorAll(`[data-key]${OWN}`)].find((y) => y.dataset.key === key);
  if (!n) { away[name] = was; if (held && newMark[name]) newMark[name].hold = true; return; }   // the row is not on screen (a direction board instead of the feed) — the position stays as it was
  const r = n.getBoundingClientRect(), top = f.getBoundingClientRect().top;
  setScroll(f, f.scrollTop + r.top - top - Math.max(0, (f.clientHeight - r.height) / 2));
  // remove the highlight with a background redraw: it waits for the owner's selection and scrolling
  setTimeout(() => { if (jump.flash === key && Date.now() >= jump.until) { jump.flash = null; renderLive(); } }, FLASH_MS + 50);
}

// the session feed rows in display order: a message sits where the session took it (delivery time), not in the middle
// of the turn during which it arrived; while queued it is pinned at the bottom, under "Working", as in Claude Code —
// it is not in the feed. Feed search (feedsearch.js) searches this same list
function feedList(name) {
  // words passed to the mini-hub (it.relayed) are not this session's message: where and when they were written
  const at = (it) => (it.kind === "in" && !it.relayed && (S.snap.messages[it.id] || {}).delivered_at) || it.ts;
  const live = S.snap.timelines[name] || [];
  return olderOf(name, live).concat(live).filter((it) => !notDelivered(it)).map((it, i) => [it, i])
    .sort((a, b) => at(a[0]) - at(b[0]) || a[1] - b[1]).map(([it]) => it);
}

// an excerpt around a search hit older than the loaded part (feedsearch.js): the feed shows it instead of itself until
// "Back to the feed" (or a jump to something loaded, or the search is closed)
let excerpt = null;   // {name, items, ts}
const EXCERPT = 50;
async function showExcerpt(name, h) {
  if (excerpt?.name === name && excerpt.items.some((it) => keyOf(it) === h.key)) { showRow(name, excerpt.items.find((it) => keyOf(it) === h.key)); return; }
  const url = (q) => `/api/history?session=${encodeURIComponent(name)}&n=${EXCERPT + 1}&${q}`;
  let a = null, b = null;
  try {
    [a, b] = await Promise.all([fetch(url(`before=${h.seq + 1}`)).then((r) => r.json()), fetch(url(`after=${h.seq}`)).then((r) => r.json())]);
  } catch { /* network — no excerpt, the jump did not happen */ }
  if (S.cur?.name !== name || !Array.isArray(a?.items)) return;
  const seen = new Set(), items = [];
  for (const it of [...a.items, ...(Array.isArray(b?.items) ? b.items.slice(0, EXCERPT) : [])].sort((x, y) => x.seq - y.seq))
    if (!seen.has(keyOf(it))) { seen.add(keyOf(it)); items.push(it); }
  const it = items.find((x) => keyOf(x) === h.key);
  if (!it) return;
  excerpt = { name, items, ts: h.ts };
  showRow(name, it);
}
function leaveExcerpt(name, quiet) {
  if (excerpt?.name !== name) return;
  excerpt = null;
  away[name] = false;   // back to the feed — from the bottom, as on entering a session
  const f = document.querySelector("main .feed"), mark = newMark[name];
  if (f) f.__pinned = true;   // the excerpt's feed is the same node, its scroll stayed at what was found
  if (mark && !mark.saw) { mark.reveal = true; mark.hold = false; }   // the new was not seen yet — to the "Last read" mark, as on opening
  if (!quiet) render();
}
setupSearch({
  items: feedList,
  top: (name) => { const live = S.snap.timelines[name] || []; const all = olderOf(name, live).concat(live); return all.length ? Math.min(...all.map((it) => it.seq)) : 1e12; },
  show: (name, it) => showRow(name, it),
  excerpt: showExcerpt,
  back: leaveExcerpt,
  inExcerpt: (name) => excerpt?.name === name,
  moved: (name) => release(name),   // the feed is scrolled to the find — do not hold the "Last read" line
});

// "New" (as in messengers): a session with unread is opened at the border of the read — above the mark the last
// read, under it the first unread, not at the end of the feed; the "To the latest" button leads to the start of the last
// answer. The border is the core's `seen` at the moment of opening. It moves when the owner has seen the new (the first unread
// stayed in the window for a second or the end was reached), not when opened: opened and left — the border stays. The mark and the button
// stay while the session is open; the next opening without news removes them. With "Open a session at the end of the feed"
// (Appearance) the mark is drawn in its place, but the session opens as without news (renderMain0, plain)
const newMark = {};   // session -> {seen, reveal: waits for the scroll to the mark (at the end — for the opening as without news), hold: the mark is held in the window until the owner scrolls, pages: the past loaded, shown, count: answers below the mark, ack: of them already shown to the owner, saw: the new was seen, upto: last_out when it was seen}
const keySeq = (n) => parseInt(n.dataset?.key, 10);   // row key — seq + kind (feed.js keyOf); a steps row has no key — NaN
// "new" by the core's value: a plugin (hub.addUnread) may have cleared it
const isUnread = (s) => s.core_unread ?? s.unread;
// New is the results that the core counts as new for the owner (a news row, core/views.py `_news`: an answer to them, not an answer
// to the hub and not a result to another session), not any answer after seen: seen is moved by the owner's word and "read", service
// answers do not move it — "New · 17" for one new answer. The mark goes before the steps of the turn of the first such result,
// after the last service message above it. There are no news rows (the core has not marked yet: a result from before the update, the feed
// is cut) — the old count: the mark right after the last read row with a key (a steps row without a key counts
// as neither read nor new), but not later than the first unread: the owner's message that stood in the queue lies in the feed by
// delivery time — after a result that came earlier (its seq is greater than seen), while the core sets seen by the send seq
const isNews = (n, seen) => !!n.dataset?.news && keySeq(n) > seen;
const isStep = (n) => !n.dataset?.key || !!n.matches?.(".row.tool, .row.sys");
const cutAt = (nodes, seen) => {
  const read = nodes.findLastIndex((n) => keySeq(n) <= seen) + 1;
  const first = nodes.findIndex((n) => isNews(n, seen));
  if (first >= 0) {
    let at = first;
    while (at > 0 && isStep(nodes[at - 1])) at--;
    return Math.min(first, Math.max(at, read));
  }
  const i = nodes.findIndex((n) => keySeq(n) > seen);
  return Math.min(i < 0 ? nodes.length : i, read);
};
// the last new answer under the mark: the button leads to the start of its byline
const lastNew = (f) => [...f.querySelectorAll(".row.newdiv ~ [data-news]")].pop() || [...f.querySelectorAll(".row.newdiv ~ .row.out")].pop();
// where to hold the feed so that the mark is in the window: in the middle (a lot of unread under the mark — it is no higher than
// the middle, the read above), and unread less than half the window — the end of the feed, the mark below the middle; if little read
// — the mark higher, as far as it goes; setScroll itself clips to the end
const lineTop = (f, dv) => Math.max(0, dv.getBoundingClientRect().top - f.getBoundingClientRect().top + f.scrollTop - f.clientHeight / 2);
// an open question card (at the end of the feed) goes into the window instead of the "New" line: the end of the feed, or
// the card's top at the top of the window when the card does not fit
function askTop(f) {
  const c = [...f.querySelectorAll(".feed-inner > .card:not(.frozen)")].find((n) => n.getClientRects().length);
  if (!c) return null;
  const at = c.getBoundingClientRect().top - f.getBoundingClientRect().top + f.scrollTop - 16;   // 16 — --space-4
  return at >= f.scrollHeight - f.clientHeight ? { top: f.scrollHeight, end: true } : { top: at, end: false };
}
// the owner took the scroll themselves (wheel, finger, bar, keys) — the mark is not held any more, the feed behaves as usual
const release = (name) => { const m = newMark[name]; if (m) m.hold = false; };
// the feed goes to the end and stays with it (the button, typing in the input): the mark is not held
function stickToEnd(f, name) {
  away[name] = false;
  release(name);
  f.__pinned = true;
}
// writes in the input or sends — the answer is at the bottom, but only while the mark is held: otherwise one who read higher is not touched. Tab, arrows and
// modifiers are not input — they do not carry the feed away (otherwise one could not Shift+Tab from the input back to the "To the latest" button)
const typing = (e) => {
  const f = e.target.closest?.(".dock") && document.querySelector("main .feed"), name = S.cur?.name;
  if (f && name && newMark[name]?.hold) stickToEnd(f, name);
};
document.addEventListener("input", typing, true);   // typing, pasting, deleting
document.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) typing(e); }, true);   // sending from the keyboard
document.addEventListener("pointerdown", (e) => { if (e.target.closest?.(".dock .send")) typing(e); }, true);   // and with the button
function openUnread(s) {
  if (isUnread(s) && s.seen > 0) newMark[s.name] = { seen: s.seen, reveal: true, pages: 0, count: 0, ack: 0 };
  else if (shownFeed !== s.name) delete newMark[s.name];   // the session vanished from the snapshot for a moment — the mark stays
}
// the session is open, the reader scrolled up, new came: they do not see it — do not mark it read until seen
// (the "New · N" mark, the "To the latest · N" button); at the bottom the reader sees the new themselves — as before
const MID_WAIT = 5000;
function midNews(s) {
  const m = newMark[s.name];   // a seen mark that the core has already moved seen past, or another result has come since, counts as absent: the border moves to the new
  if ((!m || stale(m, s) || (m.saw && s.seen > m.seen)) && isUnread(s) && s.seen > 0 && away[s.name])
    newMark[s.name] = { seen: Math.max(s.seen, m?.upto || 0), reveal: false, pages: 0, count: 0, ack: 0, mid: Date.now() };   // upto: the core's seen may not have caught up with what was sent
}
// the mark in a ready feed; the read above the first unread is not in the loaded part yet — load more of the past
function markNew(name, nodes, items) {
  const m = newMark[name];
  if (!m) return;
  const cut = cutAt(nodes, m.seen);
  const marked = nodes.some((n) => isNews(n, m.seen));   // the new is marked by the core: the past up to the read is not needed — the mark goes by it
  if (cut < nodes.length && (cut > 0 || marked)) {
    const below = nodes.slice(cut);
    m.count = below.filter((n) => (marked ? !!n.dataset?.news : n.matches?.(".row.out"))).length;   // results for the owner; steps and service rows do not count
    const first = below.find((n) => n.dataset?.key), at = first && items.find((x) => x.seq === keySeq(first))?.ts;
    const label = m.count ? t("web.main.new_count", { n: m.count }) : t("web.main.new_label");
    const since = at ? t("web.main.new_since", { time: dayTime(at) }) : "";
    const d = el("div", "row newdiv", "");
    d.setAttribute("role", "separator");
    d.setAttribute("aria-label", since ? `${label} ${since}` : label);
    d.append(el("span", "nlab", label));
    if (since) d.append(el("span", "nsince", since));
    nodes.splice(cut, 0, d);
    m.shown = true;
  } else if (loaded(name)) {
    const o = older[name];
    if (cut === 0 && m.reveal && !S.feedAtEnd && hasOlder(name) && m.pages < SEEK_PAGES) {   // at the end (Appearance) the past is not loaded for a line nobody is taken to
      if (!o?.loading && !o?.building)
        loadOlderSoon(name, () => newMark[name] === m, (r) => {
          if (r === "ok") m.pages++;
          else if (r !== "building" && r !== "busy") delete newMark[name];   // did not work (a failure, the feed was cut) — do not ask on every snapshot
        });
    } else if (!(m.mid && Date.now() - m.mid < MID_WAIT)) delete newMark[name];   // everything loaded is new and nothing is before it (or no new was found in the feed, or the owner is already scrolling) — as usual, from the bottom; a mark without a line would get in the way of marking read. What came while the session was open: the result is in the snapshot before its row in the feed — wait for it
  }
}
// "Read = seen": the page marks a session read (/api/seen) when the first unread has stayed in the window for
// SAW_MS, or the end was reached (syncNew). A background tab does not mark: the observer sees the window only of a visible one
const SAW_MS = 1000;
const watch = { name: null, node: null, timer: 0, io: null };
// a seen mark covers only the results up to upto (what was in the feed when it was seen): what came after is new until it is seen too
const stale = (m, s) => !!m.saw && s.last_out > m.upto;
function sawNew(name) {
  const m = newMark[name];
  if (!m || m.saw || S.cur?.name !== name || document.visibilityState !== "visible") return;
  m.saw = true;
  m.upto = S.cur.last_out;
  markSeen(S.cur);
}
function watchFirst(f, name) {
  const m = newMark[name], first = m && !m.saw ? f?.querySelector(".row.newdiv")?.nextElementSibling : null;
  if (!first) {
    if (watch.node) { watch.io.unobserve(watch.node); watch.node = null; }
    clearTimeout(watch.timer);
    watch.timer = 0;
    return;
  }
  if (first === watch.node) return;
  watch.io ||= new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.target !== watch.node) continue;
      if (e.isIntersecting && !watch.timer) { const n = watch.name; watch.timer = setTimeout(() => { watch.timer = 0; sawNew(n); }, SAW_MS); }
      else if (!e.isIntersecting) { clearTimeout(watch.timer); watch.timer = 0; }
    }
  });
  if (watch.node) watch.io.unobserve(watch.node);
  if (watch.name !== name) { clearTimeout(watch.timer); watch.timer = 0; }   // another session — count from the start; the same one, the feed rebuilt — the same count
  watch.name = name;
  watch.node = first;
  watch.io.observe(first);
}
// the tab is visible again — the observer reports the state anew and the count starts from the beginning
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && watch.node) { watch.io.unobserve(watch.node); watch.io.observe(watch.node); }
});
// the "To the latest" button of the session feed (jumpdown.js; a linked hub's feed has its own, hubs.js): one node, kept
// in main; on the right the number of answers that the owner has not seen yet; visible while the end of the feed
// is not shown and there is something to show
const jumper = jumpDown(() => {
  const f = document.querySelector("main .feed"), name = S.cur?.name, m = newMark[name];
  if (!f || !name) return;
  // to the start of the last answer below the mark: its byline at the top of the window; an answer shorter than the window — then the end of the feed
  const last = lastNew(f);
  const fr = f.getBoundingClientRect(), top = last ? last.getBoundingClientRect().top - fr.top + f.scrollTop - 16 : null;   // 16 — --space-4
  if (top == null || f.scrollHeight - top <= f.clientHeight) {
    stickToEnd(f, name);
    setScroll(f, f.scrollHeight);
  } else {
    away[name] = true;
    release(name);
    f.__pinned = false;
    setScroll(f, top);
  }
  // the highlight is put on the row on the page: the next snapshot draws it anew (mergeFeed does not keep it), as the
  // build draws "arrive" only while it lasts
  if (last) { jump.arrive = last.dataset.key; jump.arriveAt = Date.now(); last.classList.add("arrive"); last.__built = null; }
  if (m) m.ack = m.count;
  syncNew(f, name);
});
const newBtn = jumper.node;
function syncNew(f, name) {
  const m = f && newMark[name], main = $("main");
  // in an excerpt the end is not the end of the feed: the new is not shown by it; nor is it while the feed is still loading (an empty feed is "at the end")
  const atEnd = !!m && !m.reveal && excerpt?.name !== name && loaded(name) && f.scrollHeight - f.scrollTop - f.clientHeight < 4;
  if (atEnd) {
    m.ack = m.count;   // scrolled to the end — everything is shown, and it can be marked read
    if (!m.saw) sawNew(name);
  }
  const n = m ? m.count - m.ack : 0;
  // the button — when there are three or more new messages (the same count as in the mark) and from the mark to the end they do not fit the window:
  // one or two, even long, — the mark and the scroll are in place anyway
  const line = m && f.querySelector(".row.newdiv");
  const big = !!line && f.scrollHeight - (line.getBoundingClientRect().top - f.getBoundingClientRect().top + f.scrollTop) > f.clientHeight;
  const show = !!m && m.shown && !atEnd && m.count >= 3 && big && n > 0;
  if (show) jumper.show(n, main);
  else jumper.hide();
}

// live — the feed's .feed-inner already on the page (a redraw of the same session): question cards standing last in it
// stay in it. A card pulled out of the page loses focus and a pressed mouse button: the browser does not send a click
// to a node removed between press and release, and snapshots arrive every second — "the first click does nothing"
function feed(s, live) {
  const f = el("div", "feed"), inner = el("div", "feed-inner");
  const ex = excerpt?.name === s.name ? excerpt : null;
  if (!ex) inner.setAttribute("aria-live", "polite");   // a search excerpt has none: its entry would be read out in full
  const loadedItems = feedList(s.name);
  f.__items = loadedItems;   // the feed search (painted) gets the same list, no need to build it a second time
  const items = ex ? ex.items : loadedItems;
  const nodes = feedItems(items, S.compact, !ex && ["busy", "starting", "dialog"].includes(s.state));
  if (ex) {   // an excerpt: instead of "Show earlier" — where it comes from and the way back; the past is not loaded on scroll
    const top = el("div", "row older excerpt-head");
    const b = el("button", "btn quiet small", t("web.search.back_to_feed"));
    b.type = "button";
    b.addEventListener("click", () => leaveExcerpt(s.name));
    top.append(el("span", "hint", t("web.search.excerpt", { when: dayTime(ex.ts) })), b);
    inner.append(top, ...nodes);
    return withCards(f, inner, s, live);
  }
  if (!sought.has(s.name) && loaded(s.name)) {
    sought.add(s.name);
    if (olderKnown(s.name) !== false && !nodes.some(textBlock)) seek(s.name, nodes);   // an empty window: go earlier, until text
  }
  seekStep(s.name, nodes);
  markNew(s.name, nodes, items);
  const o = older[s.name];
  const top = el("div", "row older muted");
  if (!o && olderKnown(s.name) === false) top.hidden = true;   // nothing earlier — no button
  else if (o && !o.more) top.textContent = t("web.main.history_start");
  else if (o?.seek) top.textContent = o.building ? t("web.main.archive_building") : t("web.main.seeking_earlier");
  else {
    const b = el("button", "btn quiet small", o?.building ? t("web.main.archive_building") : t("web.main.show_earlier"));
    b.type = "button";
    b.addEventListener("click", () => { seek(s.name, nodes); render(); });
    top.append(b);
  }
  inner.append(top);
  // not while a page that has arrived waits to be drawn (renderLive holds off a redraw during a scroll): inertia would ask for the next pages meanwhile
  f.addEventListener("scroll", () => { if (f.scrollTop < 200 && anchorFor !== s.name && (older[s.name] || olderKnown(s.name) !== false)) loadOlder(s.name); },
    { passive: true });
  let stale = null;
  // a voice message being transcribed and sent: after the feed (outgoing.js). It leaves when the core has its row at
  // all — in the feed or still queued (the strip shows those, feedList leaves them out). A search excerpt has none (above)
  const outs = waitingOut(s.name, S.snap.timelines[s.name] || []);
  if (failed(s.name)) {
    // /api/timeline did not answer. The row is recreated with the feed — without role=alert (a screen reader would repeat
    // it on every event). No feed at all — no error and no button: the same status as for the stream, "Connecting to the
    // hub… · in N s" (backoff.js), the attempts go by themselves and a press tries at once; if the feed is already
    // there, it "did not refresh" and stays with its button
    if (loaded(s.name)) {
      const r = el("p", "hint feed-failed"), b = el("button", "btn quiet small", t("web.main.feed_retry"));
      b.type = "button";
      b.addEventListener("click", () => retry(s.name));
      r.append(document.createTextNode(t("web.main.feed_stale") + " "), b);
      stale = r;   // the feed exists — the row goes at the bottom, where new content arrives and where people read
    } else {
      const r = el("p", "hint feed-wait");
      showWait(r, nextTry(s.name));
      r.tabIndex = 0;
      r.setAttribute("role", "button");
      r.title = t("web.main.feed_retry");
      r.addEventListener("click", () => retry(s.name));
      r.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); retry(s.name); } });
      inner.append(r);
    }
  } else if (!items.length && !loaded(s.name)) inner.append(el("p", "hint feed-loading", t("web.main.feed_loading")));
  else if (!items.length && !outs.length && !o?.seek) inner.append(el("p", "hint", s.kind === "hub"
    ? t("web.main.feed_empty_hub")
    : t("web.main.feed_empty")));
  // a bubble after another row has the gap of a new message above it (feed.js: "turn"), as the core's row that takes its place
  inner.append(...nodes, ...outs.map((it, i) => { const n = feedItem(it); if (nodes.length || i) n.classList.add("turn"); return n; }));
  if (stale) inner.append(stale);
  if (["busy", "starting"].includes(s.state)) {
    const w = el("div", "row working"), sp = el("span", "g spin", SPIN[0]), txt = el("span", "b");
    sp.setAttribute("aria-hidden", "true");
    const tm = el("span", "muted elapsed", ` · ${elapsed(s.since)}`);
    tm.dataset.since = s.since;
    // recap — what it is working on now, like the line next to the spinner in Claude Code
    const recap = s.state === "busy" && s.recap && s.recap.ts >= s.since ? s.recap.text : "";
    txt.append(document.createTextNode(recap || (s.state === "starting" ? t("web.main.starting") : t("web.main.working"))), tm,
      el("span", "muted", s.state !== "busy" ? "" : (s.pending || []).some((r) => r.tool === "AskUserQuestion")
        ? ` · ${t("web.main.esc_close")}` : ` · ${t("web.main.esc_stop")}`));   // Esc closes an open question first
    w.append(sp, txt);
    inner.append(w);
  }
  // the session's background commands and agents, as in Claude Code (otherwise it is unclear whether it is waiting or stuck)
  for (const b of s.background || []) {
    const w = el("div", "row working bg"), sp = el("span", "g spin", SPIN[0]), txt = el("span", "b");
    sp.setAttribute("aria-hidden", "true");
    const tm = el("span", "muted elapsed", ` · ${elapsed(b.since)}`);
    tm.dataset.since = b.since;
    txt.append(el("span", "muted", (b.kind === "agent" ? t("web.main.bg_row_agent") : t("web.main.bg_row_command")) + " "),
      document.createTextNode(b.title || b.id), tm);
    txt.title = s.state === "idle" ? t("web.main.bg_idle_hint") : "";
    w.append(sp, txt);
    inner.append(w);
  }
  // executors work on behalf of an idle session — like "Working": shows who exactly, a click goes to them
  const crew = crewOf(s);
  if (crew.length) {
    const w = el("div", "row working crew"), sp = el("span", "g spin", SPIN[0]), txt = el("span", "b");
    sp.setAttribute("aria-hidden", "true");
    txt.append(document.createTextNode((crew.length === 1 ? t("web.main.crew_one") : t("web.main.crew_many", { count: crew.length })) + " "));
    crew.forEach((n, i) => {
      const a = el("a", null, shortName(n, s.name));
      a.href = "#/s/" + encodeURIComponent(n);
      a.title = n;
      txt.append(...(i ? [document.createTextNode(", "), a] : [a]));
    });
    w.append(sp, txt);
    inner.append(w);
  }
  // the queue now lives in the "Waiting for you" strip above the input field (strip.js), not at the end of the feed
  // a question to the owner comes last in the feed, inside its scroll
  return withCards(f, inner, s, live);
}
// question cards come last in the feed (in an excerpt too); those already standing in the feed stay in place
function withCards(f, inner, s, live) {
  const cards = ownerCards(s);
  if (live && cards.some((c) => c.parentNode === live)) f.__stay = cards;   // do not move them
  else inner.append(...cards);
  f.append(inner);
  return f;
}
// The feed's rows are built anew on every snapshot (during a turn — once a second), and most of them are the same. Put in
// whole, a feed of a few thousand nodes was styled and laid out again each time, and the page stayed busy. A row whose
// new markup equals the markup it was put in with stays the same node (its layout, its images and a theme's marks stay);
// a changed one is replaced by the new one. Rows are matched by the entry key, the working rows and the "New" line by
// kind and order; the stopwatch's text does not count (it ticks by itself, main.js), the look does (a theme marks the rows
// it shows and does not take its marks off: another look puts the feed in anew). A row with an image or a video is
// always the new one: the build takes those nodes out of the row on the page (feed.js pic, player). The other rows
// ("Show earlier" holds the build's rows, the reconnect countdown) are always new.
// Question cards stay in place: new cards and those that changed order are placed from the end — a card already standing
// where it should does not move (a second question in the middle of answering the first does not pull the first out).
// aria-live matches the freshly built feed (an excerpt has none), set before insertion: otherwise entering an excerpt would
// be read out in full
// the sessions' icons are a part of the look: a theme draws a replier's icon from the snapshot (Clean's reply avatars)
const faces = () => (S.snap?.sessions || []).filter((s) => s.icon).map((s) => s.name + "=" + s.icon).sort().join("|");
function stamp(nodes) {
  const seen = new Map(), look = lookSig() + "\0" + faces();
  for (const n of nodes) {
    if (n.nodeType !== 1 || !(n.dataset.key || n.matches(".row.working, .row.newdiv"))) continue;
    const i = seen.get(n.className) || 0;
    if (!n.dataset.key) seen.set(n.className, i + 1);
    n.__slot = n.dataset.key ? "k" + n.dataset.key : n.className + "#" + i;
    n.__built = n.querySelector("img, video") ? null : look + "\0" + unticked(n.outerHTML);
  }
}
function mergeFeed(live, inner, cards) {
  if (inner.hasAttribute("aria-live")) live.setAttribute("aria-live", "polite"); else live.removeAttribute("aria-live");
  const had = new Map();
  for (const o of live.children) if (o.__slot && !cards.includes(o)) had.set(o.__slot, o);
  const want = [...inner.childNodes];
  stamp(want);
  want.forEach((n, i) => {
    const o = n.__built != null && had.get(n.__slot);
    if (o && o.__built === n.__built) { adopt(o, n); want[i] = o; had.delete(n.__slot); }
  });
  const keep = new Set([...want, ...cards]);
  for (const o of [...live.childNodes]) if (!keep.has(o)) o.remove();
  let at = live.firstChild;
  for (const n of want) {
    if (n === at) at = at.nextSibling;
    else live.insertBefore(n, at);
  }
  for (let i = cards.length - 1, next = null; i >= 0; next = cards[i--]) {
    if (cards[i].parentNode !== live || cards[i].nextSibling !== next) live.insertBefore(cards[i], next);
  }
}

// an open session on a visible tab — its results count as read
const seenSent = {};
function markSeen(s) {
  // "read" follows the core's value: a plugin (hub.addUnread) may have cleared "new", but the core must still be told it was viewed
  if (!isUnread(s) || document.visibilityState !== "visible" || (seenSent[s.name] || 0) >= s.last_out) return;
  const m = newMark[s.name];
  if (m && (!m.saw || (stale(m, s) && away[s.name]))) return;   // the border is shown, but the owner has not seen the new yet (watchFirst, syncNew); what came after the seen, while they read higher up, too
  seenSent[s.name] = s.last_out;
  post("/api/seen", { session: s.name, seq: s.last_out });
}
document.addEventListener("visibilitychange", () => {
  const s = S.cur;
  if (!s) return;
  // came back to a tab where the session has been open all along, and while the tab was in the background new came (not marked in the background):
  // the border is built as on opening — otherwise there is none, since there was no opening
  if (document.visibilityState === "visible" && !newMark[s.name] && shownFeed === s.name && isUnread(s) && s.seen > 0) { openUnread(s); render(); }
  // opened at the end (Appearance) in a background tab: the first unread is above the window and the observer never reports it — the end on screen does, now that the tab is visible
  if (S.feedAtEnd && document.visibilityState === "visible" && shownFeed === s.name) syncNew(document.querySelector("main .feed:not(.boards-body)"), s.name);
  markSeen(s);
});

// the page's top row: the band of a hub not ready yet (setup.js), "the hub needs an update" (minbar.js), the core's "MCP waits for a login" band (mcpwait.js) and the strip for "topbar" plugins (ext.js); its
// own node: redrawing main does not remove it (a removed node would lose focus together with a field), renderMain puts it first
const topbar = el("div", "topbar");
topbar.setAttribute("aria-label", t("web.settings.tab.plugins"));
const topRow = el("div", "top");
topRow.append(setupBar, minBar, mcpBar, topbar);
const pinned = () => (topRow.parentNode === $("main") ? [topRow] : []);

// the panel to the right of the session — a slot for session.aside plugins (API 1.6). If a plugin leaves the slot
// empty, there is no panel. On a wide screen it is open until closed (remembered in the browser); on a narrow one it
// opens only via the header button, over the feed.
const side = el("section", "side");
side.setAttribute("aria-label", t("web.main.session_panel"));
side.hidden = true;
const narrow = matchMedia("(max-width: 760px)");
narrow.addEventListener("change", () => render());
let sideWide = localStorage.getItem("hub.side") !== "closed";
let sideSheet = null;   // narrow screen: the session whose panel is open over the feed
let sideNow = { title: "", open: false };
function setSide(name, open) {
  // already so — do nothing: a plugin calls ctx.open()/close() from its own render too, and a redraw calls render again
  if ((narrow.matches ? sideSheet === name : sideWide) === open) return;
  if (narrow.matches) sideSheet = open ? name : null;
  else { sideWide = open; localStorage.setItem("hub.side", open ? "open" : "closed"); }
  render();
}
// dividers beside the list on the left and the panel on the right (both sidebars can be moved, collapsed and pulled
// out): dragging sets the width; dragging to the edge folds (closes, for the right one); dragging from the edge pulls it
// out; a click folds or unfolds; the keyboard arrows change the width. Widths are remembered in the browser; a narrow
// screen has no dividers — there the panels open with buttons, as before
const LEFT = { min: 200, max: 520, def: 272, fold: 140 }, RIGHT = { min: 300, max: 760, fold: 220 };
let leftW = Number(localStorage.getItem("hub.leftW")) || 0;   // 0 — responsive theme default until the owner resizes
if (leftW === LEFT.def) leftW = 0;   // earlier versions wrote the default width on every fold or unfold: that is not a chosen width
let leftFolded = localStorage.getItem("hub.leftFolded") === "1";
let rightW = Number(localStorage.getItem("hub.rightW")) || 0;   // 0 — the default, from CSS
const clampW = (w, k) => Math.round(Math.min(k.max, Math.max(k.min, w)));
function applyWidths(left = leftFolded ? 0 : leftW || null, right = rightW) {
  const a = $("app");
  a.style.setProperty("--left-w", left === null ? `var(--sidebar-default-width, ${LEFT.def}px)` : `${left}px`);
  if (right) a.style.setProperty("--right-w", `${right}px`); else a.style.removeProperty("--right-w");
  a.classList.toggle("left-folded", left === 0);
}
function setLeft(w, folded) {
  const changed = leftFolded !== folded;
  leftFolded = folded;
  if (!folded && w > 0) leftW = clampW(w, LEFT);
  localStorage.setItem("hub.leftW", String(leftW));
  localStorage.setItem("hub.leftFolded", folded ? "1" : "0");
  applyWidths();
  if (changed) dispatchEvent(new CustomEvent("hub-sidebar-changed", { detail: { open: !leftFolded } }));
}
// Same layout state as the divider: no navigation or view rebuild.
addEventListener("hub-sidebar-request", (event) => {
  const request = event.detail;
  if (!request || typeof request !== "object") return;
  if (!narrow.matches && typeof request.open === "boolean" && request.open === leftFolded) setLeft(leftW, !request.open);
  request.result = { open: !leftFolded, available: !narrow.matches };
});
function setRight(w) {   // w below the threshold — close the panel; otherwise open it at this width
  const name = sideNow.name;
  if (w < RIGHT.fold) return setSide(name, false);
  rightW = clampW(w, RIGHT);
  localStorage.setItem("hub.rightW", String(rightW));
  applyWidths();
  if (!sideNow.open) setSide(name, true);
}
function grip(which) {
  const g = el("div", `grip ${which}`), left = which === "left";
  g.setAttribute("role", "separator");
  g.setAttribute("aria-orientation", "vertical");
  g.setAttribute("aria-label", left ? t("web.main.grip_left") : t("web.main.grip_right"));
  g.tabIndex = 0;
  g.title = left ? t("web.main.grip_left_hint") : t("web.main.grip_right_hint");
  const fromLeft = left !== (pageDir === "rtl");   // a right-to-left page has the session list on the right
  const widthAt = (x) => (fromLeft ? x : innerWidth - x);
  const now = () => (left ? (leftFolded ? 0 : $("app").querySelector("aside").getBoundingClientRect().width) : sideNow.open ? $("app").querySelector(".side").getBoundingClientRect().width : 0);
  const toggle = () => (left ? setLeft(leftW, !leftFolded) : setSide(sideNow.name, !sideNow.open));
  let drag = null;
  const move = (e) => {
    if (!drag) return;
    drag.moved = drag.moved || Math.abs(e.clientX - drag.x) > 3;
    if (!drag.moved) return;
    const w = Math.max(0, widthAt(e.clientX));
    if (left) applyWidths(Math.min(LEFT.max, w), rightW);
    else if (sideNow.open) applyWidths(leftFolded ? 0 : leftW || null, Math.min(RIGHT.max, Math.max(RIGHT.fold, w)));
    g.classList.toggle("folding", w < (left ? LEFT.fold : RIGHT.fold));
  };
  const up = (e) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    document.body.classList.remove("resizing");
    g.classList.remove("dragging", "folding");
    const d = drag;
    drag = null;
    if (!d) return;
    if (!d.moved) return toggle();
    const w = widthAt(e.clientX);
    if (left) setLeft(w, w < LEFT.fold);
    else setRight(w);
  };
  g.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    drag = { x: e.clientX, moved: false };
    g.classList.add("dragging");
    document.body.classList.add("resizing");
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  });
  g.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 64 : 16, dir = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
    if (dir) { e.preventDefault(); const w = now() + (fromLeft ? dir : -dir) * step; return left ? setLeft(w, w < LEFT.fold) : setRight(w); }
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  return g;
}
const grips = { left: grip("left"), right: grip("right") };
applyWidths();

function renderSide(s) {
  const nodes = s ? slotNodes("session.aside", { session: s, close: () => setSide(s.name, false),
    open: () => setSide(s.name, true) }) : [];
  // the same nodes are not rearranged: an input field in the panel keeps its focus and text
  if (nodes.length !== side.children.length || nodes.some((n, i) => side.children[i] !== n)) side.replaceChildren(...nodes);
  for (const n of nodes) n.hidden = !n.childNodes.length;
  const live = nodes.filter((n) => !n.hidden);
  const open = !!live.length && (narrow.matches ? sideSheet === s.name : sideWide);
  side.hidden = !open;
  $("app").classList.toggle("with-side", open);
  if (side.parentNode !== $("app")) $("app").append(side);
  sideNow = { title: live.length ? live[0].dataset.title || t("web.main.panel") : "", open, name: s?.name };
  for (const g of Object.values(grips)) if (g.parentNode !== $("app")) $("app").append(g);
  grips.left.hidden = narrow.matches;
  grips.right.hidden = narrow.matches || !sideNow.title;   // a session without a panel has nothing to pull out
  return sideNow;
}

function renderMain() {
  renderMain0();
  if (!S.cur) renderSide(null);
  const main = $("main"), nodes = slotNodes("topbar");
  const toolbar = S.selected?.startsWith(":x:") ? extToolbar(S.selected.slice(3)) : null;
  if (toolbar) nodes.push(toolbar);
  placeNodes(topbar, nodes);   // plugin nodes stay in place: a field in the strip keeps its focus
  topbar.hidden = !nodes.length;
  renderMcp(S.snap?.mcp_waits, S.snap?.mcp_logins);
  renderSetup();
  renderMin();
  topRow.hidden = topbar.hidden && mcpBar.hidden && setupBar.hidden && minBar.hidden;
  if (main.firstChild !== topRow) main.prepend(topRow);
}

// a feed pinned to the bottom stays pinned when its height changes after a redraw: the input field grew (tasks, status
// line), a row was appended or an image loaded. Otherwise the bottom row ("A command is running in the background: …")
// slides under the field and is visible only after scrolling
let followRO = null;   // the feed is new on every redraw — the previous observer is disconnected
// the owner reads higher up: once they scroll up even a little, the feed does not return to the bottom on a redraw or
// on growth until they scroll down to the bottom themselves. Previously anything within 120 px counted as "at the
// bottom", and a working session (events every second) cancelled the first wheel movements upward
const NEAR = 24;     // "at the very bottom", px: closer — the feed follows new content, farther — it stands still while the owner reads
const away = {};   // session name -> scrolled up
S.reading = (name) => !!away[name] && S.cur?.name === name;   // timeline.js: while reading higher up, rows are not discarded
// the start of the live feed has gone — what was loaded earlier no longer joins up with it; a running text search
// continues from the new top (its page limit stays as it was)
S.trimmed = (name) => { const sk = older[name]?.seek; delete older[name]; if (sk) olderAt(name).seek = sk; searchTrimmed(name); };
// the row at the top of the feed window and its offset: after a redraw the same row stays at the same place (everything above it may have changed)
function rowAt(f) {
  const top = f.getBoundingClientRect().top;
  for (const n of f.querySelectorAll(`[data-key]${OWN}`)) {
    const r = n.getBoundingClientRect();
    if (r.bottom > top) return { key: n.dataset.key, y: r.top - top };
  }
  return null;
}
// the question card being read stays put: new rows land above it, and the place used to be kept by a row above the card,
// so the card got pushed down, and at the bottom the queue strip made it jump. Hold the card itself while it is in view
// and the feed was at the bottom, it is already held, or it starts in the upper half of the view
const holding = {};
function cardAt(f, name, stick) {
  const box = f.getBoundingClientRect();
  const c = [...f.querySelectorAll(".feed-inner > .card")].find((n) => {
    const r = n.getBoundingClientRect();
    return r.bottom > box.top && r.top < box.bottom;
  });
  const y = c ? c.getBoundingClientRect().top - box.top : 0;
  holding[name] = !!c && (stick || holding[name] || y < f.clientHeight / 2);
  return holding[name] ? { node: c, y } : null;
}
const gapOf = {};  // session name -> distance to the bottom at the last scroll (the feed was recreated — restore the same place)
let shownFeed = null;   // the session whose feed was last on the page (only "empty" redraws happened in between)
// the page reloaded by itself (the hub updated, web restarted): a reader higher up returns to the same place, not to
// the bottom. The place is the distance from the bottom of the feed
const SCROLL_KEY = "hub.scroll";
function savedScroll() {
  try {
    const v = JSON.parse(sessionStorage.getItem(SCROLL_KEY) || "null");
    return v && Date.now() - v.ts < 600000 ? v : null;
  } catch { return null; }
}
let restore = savedScroll();   // {name, gap, ts}: waits until the feed gains height (the snapshot arrives in parts)
if (restore) restore.until = Date.now() + 10000;
function rememberScroll(f, name) {
  try {
    const gap = f.scrollHeight - f.scrollTop - f.clientHeight;
    if (away[name] && gap > 4) sessionStorage.setItem(SCROLL_KEY, JSON.stringify({ name, gap: Math.round(gap), ts: Date.now() }));
    else if (savedScroll()?.name === name) sessionStorage.removeItem(SCROLL_KEY);
  } catch { /* storage is unavailable — no restoring */ }
}
// whether the feed is pinned to the bottom — kept on the node itself: it lives across redraws, and there is a single scroll listener on it
function follow(f, pinned, name) {
  f.__pinned = pinned;
  followRO?.disconnect();
  followRO = new ResizeObserver(() => {
    const dv = newMark[name]?.hold && f.querySelector(".row.newdiv");   // the feed grew after opening — the line stays in the window
    if (dv) setScroll(f, lineTop(f, dv));
    else if (f.__pinned) setScroll(f, f.scrollHeight);
  });
  followRO.observe(f);
  followRO.observe(f.firstElementChild);
  if (f.__follows) return;
  f.__follows = true;
  let last = f.scrollTop;
  f.addEventListener("scroll", () => {
    const gap = f.scrollHeight - f.scrollTop - f.clientHeight;
    // last — the lowest point where the feed was pinned: one-pixel steps up (scrollbar, arrows) each fit within the
    // tolerance but together lead away from the bottom — so we count from it, not from the previous event
    if (f.scrollTop < last - 1) away[name] = true;
    if (gap < 4) away[name] = false;
    // at the bottom — count anew: the feed got shorter (the browser lowered the position), and the previous point is above the real bottom
    last = away[name] || gap < 4 ? f.scrollTop : Math.max(last, f.scrollTop);
    f.__top = f.scrollTop;
    gapOf[name] = gap;
    if (f.__own) { f.__own = false; release(name); }   // the scroll is not ours (state.js): the bar, keys, search on the page
    // pinned stays until the owner scrolls up: the page's own scroll to the bottom can land short when the dock grows
    // before the next frame — the scroll event comes before ResizeObserver and saw the gap (otherwise the owner would
    // have to scroll down to the last message on coming back to a session)
    f.__pinned = !away[name] && !newMark[name]?.hold && (f.__pinned || gap < NEAR);
    if (!f.dataset.restoring) rememberScroll(f, name);
    syncNew(f, name);
  }, { passive: true });
  // intent comes before the scroll: wheel up or a finger on the feed — do not pull down, even if the scroll event has not arrived yet
  // and stop holding the question card until it comes into view again
  f.addEventListener("wheel", (e) => { if (e.deltaY) release(name); if (e.deltaY < 0) { away[name] = true; f.__pinned = false; holding[name] = false; if (newMark[name]) newMark[name].reveal = false; } }, { passive: true });
  f.addEventListener("touchmove", () => { release(name); f.__pinned = false; holding[name] = false; if (newMark[name]) newMark[name].reveal = false; }, { passive: true });
}

// a feed video in fullscreen: a redraw moves the player into a new row, and the browser exits fullscreen for an element
// removed from the document (so background updates would kick the viewer out) — the feed waits until fullscreen ends
function fullVideo() {
  const main = $("main"), f = document.fullscreenElement || document.webkitFullscreenElement;
  return !!(f && main.contains(f)) || [...main.querySelectorAll("video")].some((v) => v.webkitDisplayingFullscreen);
}
for (const kind of ["fullscreenchange", "webkitfullscreenchange", "webkitendfullscreen"])   // the last one is iPhone, for <video>
  document.addEventListener(kind, () => { if (!fullVideo()) render(); }, true);

function renderMain0() {
  const main = $("main");
  loadExtensions().then((fresh) => { if (fresh) render(); });   // a new plugin — its item, slots and view (on Flow too)
  // "Settings": the cursor is in a tab's field (plugin search, a plugin's section) — do not rebuild, or the field loses focus
  if (S.selected === ":settings" && main.dataset.for === ":settings" && document.activeElement?.closest?.("main .feed")
      && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (fullVideo()) return;
  if (S.selected && S.selected.startsWith(":x:")) {   // an extension view (EXTENDING.md)
    S.cur = null;
    shownFeed = null;
    $("app").dataset.view = "session";
    const root = extView(S.selected.slice(3));
    if (!root || main.dataset.for !== S.selected || !main.contains(root))   // the same view — do not rearrange (canvas)
      placeNodes(main, [...pinned(), $("banner"), main.querySelector(".drop"), root || (extLoading() ? empty(t("web.main.loading"), "")
        : empty(t("web.main.no_view"), t("web.main.no_view_hint", { settings: t("web.settings.tab.general"), plugins: t("web.settings.tab.plugins") })))]);
    main.dataset.for = S.selected;
    return;
  }
  extHide();
  if (S.selected?.startsWith(":hub:")) {   // a linked hub's page: its parts are kept across snapshots (the field keeps its text)
    S.cur = null;
    shownFeed = null;
    $("app").dataset.view = "session";
    placeNodes(main, [...pinned(), $("banner"), main.querySelector(".drop"), ...hubView(S.selected.slice(5))]);
    main.dataset.for = S.selected;
    return;
  }
  if (S.selected === ":schedule" || S.selected === ":settings" || S.selected === ":updates" || S.selected === ":kit") {
    const ta = main.querySelector("#msg");
    if (ta && main.dataset.for) S.drafts[main.dataset.for] = ta.value;
    const old = main.querySelector(".feed"), top = old && main.dataset.for === S.selected ? old.scrollTop : 0;
    S.cur = null;
    shownFeed = null;
    $("app").dataset.view = "session";
    const view = S.selected === ":schedule" ? scheduleView() : S.selected === ":updates" ? updatesView()
      : S.selected === ":kit" ? kitView() : settingsView();
    placeNodes(main, [...pinned(), $("banner"), main.querySelector(".drop"), ...view.childNodes, el("div")]);
    main.dataset.for = S.selected;
    main.querySelector(".feed").scrollTop = top;
    return;
  }
  const hub = S.snap.sessions.find((x) => x.kind === "hub" && !resting(x));   // a sleeping hub with a question can be opened
  const none = !S.selected || S.selected === ":sessions";   // no session selected: an empty address opens the hub feed
  const s = S.snap.sessions.find((x) => x.name === S.selected) || (none ? hub : undefined);
  S.cur = s || null;
  // on a phone the root is the hub feed (data-view=session), the session list is at #/sessions or when there is no hub
  $("app").dataset.view = S.selected === ":sessions" ? "list" : S.selected && s ? "session" : none && s ? "session" : "list";
  const keep = [...pinned(), $("banner"), main.querySelector(".drop")];
  if (!s) {
    const has = S.snap.sessions.length;
    placeNodes(main, [...keep, el("div"), empty(has ? (S.selected ? t("web.main.no_such", { name: sessionName(S.selected) }) : t("web.main.choose")) : t("web.main.none_yet"),
      has ? t("web.main.gone_hint") : t("web.main.none_hint")), el("div")]);
    delete main.dataset.for;
    return;
  }
  // the previous feed; a direction board is not one: back from the board the feed opens as on entering the session —
  // at the bottom (the board's scroll was taken for the feed's, and a tall board opened the feed at the start)
  const old = main.querySelector(".feed:not(.boards-body)");
  if (anchorFor) {
    if (anchorFor === s.name && old && main.dataset.for === s.name) S.anchor = old.scrollHeight - old.scrollTop;
    anchorFor = null;
  }
  // the feed was removed and returned (the session vanished from the snapshot for a moment), and the owner was reading above the bottom — the same place, not the bottom
  const resume = main.dataset.for !== s.name && shownFeed === s.name && away[s.name] && gapOf[s.name] > NEAR ? gapOf[s.name] : null;
  if (main.dataset.for !== s.name && resume == null) openUnread(s);   // not on returning to the former place: the line does not carry off one who read higher up
  else if (main.dataset.for === s.name) midNews(s);
  if (resume == null && (!old || main.dataset.for !== s.name)) away[s.name] = false;   // a session was opened — from the bottom
  if (restore && ownerScrolled()) restore = null;   // scrolled by themselves after the reload — the place is theirs now, do not restore
  const back = restore && restore.name === s.name && Date.now() < restore.until ? restore : null;   // return to the place after a reload
  if (restore && restore.name === s.name && !back) { restore = null; if (!old) away[s.name] = false; }   // the height did not arrive in time — as usual, from the bottom
  if (back) away[s.name] = true;
  // a scroll up that the scroll event has not delivered yet (PageUp, Home, the scrollbar): the pin is stale. A scroll
  // that hit the bottom (the feed became shorter for a moment) does not count
  const gapNow = old ? old.scrollHeight - old.scrollTop - old.clientHeight : 0;
  const jumpedUp = !!old && old.__top != null && old.scrollTop < old.__top - 1 && gapNow > 1;
  const stick = !back && resume == null && (!old || main.dataset.for !== s.name || (!away[s.name] && !jumpedUp && (old.__pinned || gapNow < NEAR)));
  const prevTop = old ? old.scrollTop : 0;
  const hadFocus = document.activeElement?.tagName === "TEXTAREA";
  // the cursor is in a question card ("own answer") or in a plugin field in the session header: the node moves into the
  // new feed/header (the header is rebuilt) — restore focus and the caret position
  const inCard = document.activeElement?.closest?.(".feed .card, .head .slot") ? document.activeElement : null;
  const caret = inCard && "selectionStart" in inCard ? [inCard.selectionStart, inCard.selectionEnd] : null;

  // a direction board instead of the feed: the same node across redraws — scroll and the open card stay in place
  const board = (S.snap.directions || []).includes(s.name) && boardOpen(s.name);
  if (board) delete newMark[s.name];   // a board builds no feed and no "New" border to wait for: opening it counts as read, as before the mark
  const kept = board ? [...main.querySelectorAll(".boards-body")].find((n) => n.dataset.slug === s.name) : null;
  const same = !board && old && main.dataset.for === s.name ? old : null;
  // the reader's place is taken before the new feed is built: the question card moves into it as the same node, the
  // visible feed is shorter by the card for a moment, and the first layout read used to cut the scroll by its height
  // (the view rolled back when a question appeared or someone wrote)
  const card = same && S.anchor == null && !back ? cardAt(same, s.name, stick) : null;
  const pin = same && !stick && !card && S.anchor == null && !back ? rowAt(same) : null;
  renderSide(s);   // before the header: it holds the panel button
  const liveInner = same?.querySelector(":scope > .feed-inner");
  const fresh = board ? boardFrame(s.name, kept) : feed(s, liveInner);
  // the same session — the feed stays the same node, only its content changes: scrolling (and trackpad inertia)
  // continues by itself, the position is neither read nor set anew. Previously every event of a working session
  // replaced the feed with a new node at a position read with a lag, and a step up was rolled back
  if (same && liveInner) mergeFeed(liveInner, fresh.firstElementChild, fresh.__stay || []);
  else if (same) same.replaceChildren(...fresh.childNodes);
  else if (!board) stamp(fresh.firstElementChild.children);   // a feed put in anew: its rows are matched from the next snapshot
  const f = same || fresh, stay = kept || same;
  const key = dockKey(s);
  const oldDock = main.querySelector(".dock");
  const low = oldDock && oldDock.dataset.key === key ? oldDock : dock(s);   // the field is not recreated without need
  low.dataset.key = key;
  if (low === oldDock || stay) {
    // the field stays in place: an element removed from the page loses focus
    // the feed search row is the same node, do not remove it: typed text and focus (feedsearch.js)
    for (const n of [...main.children]) if (!keep.includes(n) && n !== low && n !== stay && n !== newBtn && !n.classList.contains("fsearch")) n.remove();
    if (stay) { stay.before(head(s)); if (low !== oldDock) stay.after(low); } else low.before(head(s), f);
  } else {
    placeNodes(main, [...keep, head(s), f, low]);
  }
  main.dataset.for = s.name;
  // the field's strip, tasks and status line first: they take their height from the feed, and the fold is measured against
  // the feed as it stays (measured before them, a message just over the bar came whole; a later draw does not measure a cut anew)
  refreshTodosAndStatus(low, s);
  if (!board) foldTall(f);   // long messages are measured on the page, before the reading place is set
  markSeen(s);
  watchFirst(board ? null : f, s.name);
  if (!board) {
    const room = back ? f.scrollHeight - f.clientHeight - back.gap : -1;   // there is enough height — the place exists
    if (back && room >= 0) { f.dataset.restoring = "1"; restore = null; }
    const mk = newMark[s.name], dv = mk?.reveal || mk?.hold ? f.querySelector(".row.newdiv") : null;   // the "Last read" line appeared in the feed
    // "Open a session at the end of the feed" (Appearance): the line is not gone to — the feed opens as without news (the place after a reload,
    // otherwise the end); an open question card goes first, but not before the place after a reload
    const ask = dv && mk.reveal && !(S.feedAtEnd && back) ? askTop(f) : null;
    const plain = !ask && !!dv && mk.reveal && S.feedAtEnd;
    if (plain) mk.reveal = false;
    const top = ask ? ask.top : dv && !plain ? lineTop(f, dv) : S.anchor != null ? f.scrollHeight - S.anchor : resume != null ? f.scrollHeight - f.clientHeight - resume : back ? (room >= 0 ? room : f.scrollHeight) : stick && !card ? f.scrollHeight : same ? null : prevTop;
    if (top != null) {
      setScroll(f, top);
      if (ask) { mk.reveal = false; away[s.name] = !ask.end; }
      else if (dv && !plain) { mk.reveal = false; mk.hold = true; away[s.name] = f.scrollHeight - f.scrollTop - f.clientHeight > NEAR; }
    } else if (card || pin) {
      const n = card ? (f.contains(card.node) ? card.node : null) : [...f.querySelectorAll(`[data-key]${OWN}`)].find((x) => x.dataset.key === pin.key);
      const dy = n ? n.getBoundingClientRect().top - f.getBoundingClientRect().top - (card || pin).y : 0;
      if (Math.abs(dy) >= 1) setScroll(f, f.scrollTop + dy);
    }
    follow(f, ask ? ask.end : S.anchor == null && stick && !back && !card && (!dv || plain), s.name);
    shownFeed = s.name;
    syncNew(f, s.name);   // after the scroll: until the feed settles, the end is "not shown" and the button would blink on every new answer
    if (f.dataset.restoring) setTimeout(() => { delete f.dataset.restoring; rememberScroll(f, s.name); }, 300);
  } else syncNew(null, s.name);   // a board instead of the feed — the "To the latest" button goes away
  painted(board ? null : f, board ? null : s.name, fresh.__items);   // feed search: highlight matches in the new feed
  S.anchor = null;
  const input = low.querySelector("textarea");
  if (hadFocus && low !== oldDock && input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  if (inCard && document.contains(inCard) && document.activeElement !== inCard) {
    inCard.focus({ preventScroll: true });
    if (caret) { try { inCard.setSelectionRange(...caret); } catch { /* not a text field */ } }
  }
}

onRender(() => { renderMain(); renderList(); busNews(); });

window.addEventListener("hub-theme", () => { if (S.snap) render(); });
window.addEventListener("hub-kit", () => { if (S.snap && S.selected === ":kit") render(); });   // "Skills and MCP" answered   // theme icons and avatars — redraw
// pane swap when moving between sessions: the built-in View Transition, opacity only (css/base.css);
// without support, with "reduce motion", or if the address did not change — immediately, as before. A new transition replaces the previous one
function swap(fn, changed) {
  if (!changed || !document.startViewTransition || matchMedia("(prefers-reduced-motion: reduce)").matches) return fn();
  const root = document.documentElement;
  root.classList.add("vt-pane");
  const t = document.startViewTransition(fn);
  t.finished.finally(() => { if (root.__vt === t) root.classList.remove("vt-pane"); }).catch(() => {});
  t.ready.catch(() => {});             // interrupted by a new transition: "Transition was skipped" is not a page error
  t.updateCallbackDone.catch(() => {});
  root.__vt = t;
}
window.addEventListener("hashchange", () => {
  const prev = S.selected;
  S.selected = readHash();   // the "Reply" target stays with its own thread (drafts.js)
  swap(() => { render(); sync(S.cur?.name || S.selected); }, prev !== S.selected);
});
S.selected = readHash();

// keys: ⌘K — find a session, ⌘I (Ctrl+I) — cursor into the hub's input field, Esc — close a question without an answer if one is open, otherwise stop the turn
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); focusSearch(); return; }
  // ⌘F — search the open session's feed; when already in it — the browser's search (ours does not take it away for good)
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.code === "KeyF" && S.cur && $("main").dataset.for === S.cur.name
      && $("main").querySelector(".feed:not(.boards-body)") && !document.querySelector("dialog[open]") && !document.activeElement?.closest?.(".fsearch")) {
    e.preventDefault();
    openSearch(S.cur.name);
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.code === "KeyI") {   // by key code: works in any layout
    const ta = $("msg");   // the hub's main field; on pages without it (Settings, Schedule…) the shortcut is not ours
    if (!ta || e.shiftKey || e.altKey || e.isComposing || document.querySelector("dialog[open]")) return;
    e.preventDefault();
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);   // the draft is intact, the caret goes to the end
    return;
  }
  const ask = e.key === "Escape" && !e.defaultPrevented && !document.querySelector("dialog[open]")
    && document.activeElement !== $("filter") && S.cur && document.querySelector(`.ask-card[data-asker="${CSS.escape(S.cur.name)}"]:not([hidden]):not(.frozen):not(.sent)`);   // it does not close another's card (one shown by the hub)
  if (ask) { e.preventDefault(); dismissAsk(ask); return; }
  if (e.key === "Escape" && !e.defaultPrevented && !document.querySelector("dialog[open]") && document.activeElement !== $("filter")
      && S.cur && ["busy", "dialog"].includes(S.cur.state)) { e.preventDefault(); interrupt(S.cur); }
});

// small live bits: the turn stopwatch. The spinner of a working session turns by CSS (base.css .spin): a text change
// every 100 ms relaid the whole page out ~600 times a minute. Only a changed text is written: past an hour the stopwatch
// shows minutes, and a write of the same text still relays the row out and wakes a theme's observer. The text node itself
// gets the new text, not a new node in its place: a new node is a change of the page's tree, and a theme's :has() rules
// made the browser restyle the whole page for it every second. A theme may cut the " · " in front (Clean does) — the
// time is compared without it
const tick = (n, v) => {
  if (n.textContent === v) return;
  const tn = n.firstChild;
  if (tn?.nodeType === 3 && !tn.nextSibling) tn.nodeValue = v; else n.textContent = v;
};
setInterval(() => {
  for (const n of document.querySelectorAll(`.elapsed${OWN}`)) {
    const v = elapsed(Number(n.dataset.since));
    if (n.textContent.replace(/^ · /, "") !== v) tick(n, ` · ${v}`);
  }
  for (const n of document.querySelectorAll(`.step-run[data-since]${OWN}`)) tick(n, stepText(n.dataset.tool, Number(n.dataset.since), !!n.dataset.head));
  for (const n of document.querySelectorAll(`[data-next]${OWN}`)) tick(n, inTime(Number(n.dataset.next)));
}, 1000);
setInterval(() => {  // "5 min ago" on messages grows by itself
  for (const n of document.querySelectorAll(`.ago${OWN}`)) tick(n, ago(Number(n.dataset.ts)));
}, 30000);

bindSearch();
bindAttachments();
connect();

applySavedTextScale();   // the text size from settings — also before the first render
applySavedTheme();   // the selected theme (the core's, a plugin's or its own; none — Clean) — before the first render, without flicker
registerBuiltIn("core:clean", "/static/clean/", registerClean);   // after the theme: Clean's code reads its class
reconcileSavedTheme();   // then: the plugin that provided it may no longer be in the catalog — check and drop it
bindModuleViews();   // HTML views of core modules inside frames — wired to their own module's endpoints (modview.js)
