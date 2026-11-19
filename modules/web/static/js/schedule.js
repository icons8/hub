// Schedule: all the machine's jobs — upcoming ones by day, paused ones (awaiting a decision), finished ones. The clock is a separate process.
import { el, icon, kit } from "./dom.js";
import { S, render, sessionLabel } from "./state.js";
import { banner, post } from "./api.js";
import { confirmDlg, toList } from "./dialogs.js";
import { nameOf } from "./who.js";
import { locale, t } from "./i18n.js";
import { keepOpen } from "./keepopen.js";

// who set the job: the core gives the owner and the core as codes (core/views.py schedule_view)
const OWNER = ":owner", CORE = ":core";
// whose job it is: a session's jobs belong to it; a command belongs to the session it was handed to (retarget writes
// session), otherwise to the one that set it (by), as long as that session exists;
// a command of the owner, of the core or of a departed session belongs to no one and appears in the general Schedule.
// One rule for the session header, its schedule, the overview and the rows (so a session's command was not visible only in the general one)
export const jobOf = (j) => { const o = j.command ? j.session || j.by : j.session;   // = core/schedule.owner_of
  return !j.command || (S.snap.sessions || []).some((x) => x.name === o) ? o : ""; };

const CLOCK = {
  waiting: [t("web.schedule.clock_waiting"), "dialog"],
  down: [t("web.schedule.clock_down"), "failed"],
};

const hm = (ts) => new Date(ts * 1000).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
const dhm = (ts) => new Date(ts * 1000).toLocaleString(locale, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
function dayName(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const diff = Math.round((new Date(d.toDateString()) - new Date(today.toDateString())) / 86400000);
  if (diff === 0) return t("web.schedule.today");
  if (diff === 1) return t("web.schedule.tomorrow");
  return d.toLocaleDateString(locale, { weekday: "long", day: "numeric", month: "long" });
}
export function inTime(ts) {
  const s = Math.round(ts - Date.now() / 1000);
  if (s < 60) return t("web.schedule.in_now");
  // by the rounded number, so 59 min 40 s is "in 1 h" and 23 h 55 min is "in 1 day", not "in 60 min" or "in 24 h"
  if (Math.round(s / 60) < 60) return t("web.schedule.in_min", { n: Math.round(s / 60) });
  if (Math.round(s / 3600) < 24) return t("web.schedule.in_h", { h: Math.round(s / 3600) });
  return t("web.schedule.in_days", { count: Math.round(s / 86400) });
}


function remove(j) {
  confirmDlg(t("web.schedule.remove_title", { id: j.id }), t("web.schedule.remove_body", { what: j.command
    ? t("web.schedule.remove_cmd") : t("web.schedule.remove_session", { session: j.session }) }),
    t("web.schedule.remove"), async () => { const r = await post("/api/schedule/remove", { id: j.id }); if (r.error) banner(r.error); });
}

function retarget(j) {
  const f = el("form", "retarget");
  const sel = el("select");
  sel.setAttribute("aria-label", t("web.schedule.retarget_label", { id: j.id }));
  sel.append(el("option", null, t("web.schedule.retarget_placeholder")));
  sel.firstChild.value = "";
  for (const s of S.snap.sessions.filter((x) => !["failed", "closed"].includes(x.state))) {
    const o = el("option", null, sessionLabel(s));
    o.value = s.name;
    sel.append(o);
  }
  const b = el("button", "btn", t("web.schedule.retarget_btn"));
  b.type = "submit";
  f.append(sel, b);
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!sel.value) { sel.focus(); return; }
    const r = await post("/api/schedule/retarget", { id: j.id, session: sel.value });
    if (r.error) banner(r.error);
  });
  return f;
}

// one task is one item (the same tasks are not shown as separate items — show how the schedule is configured and when
// the other runs will happen): a group's jobs are lines of "how it is configured", the runs are a list
function runsText(times) {
  const shown = times.slice(0, 5).map((ts) => {
    const d = new Date(ts * 1000), today = new Date();
    return d.toDateString() === today.toDateString() ? hm(ts)
      : d.toLocaleString(locale, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  });
  return shown.length ? t("web.schedule.runs_next", { list: `${shown.join(", ")}${times.length > 5 ? "…" : ""}` }) : "";
}

// the gist of a job — the first phrase of the prompt without parenthesized details (chat IDs, addresses), so the list stays readable
// a line reads as a phrase when two of its words have letters ("Run 3 checks", "Check #42 status"), or it is a run of kana/kanji;
// a single token like `run-mac.log;` or `DONE2` is a fragment
const isPhrase = (x) => x.split(/\s+/).filter((w) => /\p{L}{2,}/u.test(w)).length >= 2
  || /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]{3,}/u.test(x);
function gistOf(text) {
  // the first line that reads as a phrase (two words, or a run of kana/kanji): a fragment like `.";` is not a title; no phrase at all — the first line
  const first = (text.split("\n").map((x) => x.trim()).find((x) => isPhrase(x)) || (text.split("\n").map((x) => x.trim()).find(Boolean) || ""));
  const cut = [" (", ". ", "; "].map((m) => first.indexOf(m)).filter((i) => i >= 15).sort((a, b) => a - b)[0];
  const g = cut != null ? first.slice(0, cut) : first;
  return g.length > 140 ? g.slice(0, 139).trimEnd() + "…" : g;
}

// a job's title: the id of a command (or its readable text), or the gist of the text cut at a word (the path and the full text go inside)
function titleOf(j) {
  if (j.command && !(j.prompt || "").trim()) return j.id;   // never a piece of the command: it is shown whole inside
  const g = gistOf(j.prompt || "") || j.id;
  return g.length > 70 ? g.slice(0, 68).replace(/\s+\S*$/, "") + "…" : g;
}

// one task is one row: time, title and cadence; the text, the command, the next runs and "Remove" are inside;
// `key` keeps the row open across snapshots: a group's is its own name, not its next job (an earlier run would close it);
// another row's has its state: a paused row opens by default, and that opening must not follow the job once it is
// handed over or removed
function jobRow(j, js = [j], key = `${j.state}:${j.id}`) {
  const li = el("li", "job");
  li.dataset.s = j.state;
  const o = jobOf(j);
  const live = ["active", "paused", "expired"].includes(j.state);
  const d = el("details", "fold");
  keepOpen(d, "sched:" + key, j.state === "paused" || j.state === "expired");   // a decision is waiting — open
  const sum = el("summary");
  const when = el("div", "when");
  if (j.state === "active" && j.next) {
    when.append(el("span", "t", hm(j.next)));
    const rel = el("span", "rel", inTime(j.next));
    rel.dataset.next = j.next;
    when.append(rel);
  } else if (j.state === "paused" || j.state === "expired") {
    when.append(el("span", "t", t(j.state === "paused" ? "web.schedule.paused" : "web.schedule.expired_tag")));
  }
  const what = el("div", "what");
  const text = j.command ? j.command : j.prompt || "";
  const cad = el("div", "cad");
  cad.append(el("span", null, js.length > 1 ? t("web.schedule.runs_planned", { count: js.length }) : j.when));
  if (j.command && j.last_exit) cad.append(" · ", el("span", "bad", t("web.schedule.last_err", { code: j.last_exit })));
  const title = el("p", "prompt", titleOf(j));
  what.append(title, cad);
  sum.append(when, what);
  const body = el("div", "body");
  // the full text only when the title is not all of it; a command is shown as itself
  if (j.command) body.append(el("code", null, j.command));
  else if (text.trim() !== title.textContent) body.append(el("p", "full", text));
  if (j.check) body.append(el("code", null, t("web.schedule.precheck", { check: j.check })));
  if (j.state === "paused") body.append(el("p", "why", t("web.schedule.paused_why", { why: j.paused_why || t("web.schedule.on_pause") })));
  if (j.state === "expired" && j.until) body.append(el("p", "why", t("web.schedule.expired_at", { date: dhm(j.until) })));
  else if (!live) body.append(el("p", "why muted", j.label));
  if (!S.scheduleFor && o) {
    const who = el("a", "who", t("web.schedule.schedule_of", { name: nameOf(o) }));
    who.href = "#/schedule/" + encodeURIComponent(o);
    body.append(who);
  }
  // who set it — only when it is not the session itself
  if (j.by && j.by !== o && j.by !== j.session && j.by !== OWNER) body.append(el("p", "by", t("web.schedule.set_by", {
    by: j.by === "hub" ? t("web.schedule.by_hub") : j.by === CORE ? t("web.schedule.by_core") : j.by })));
  // several jobs of one task: each with its own cadence and its own "Remove"
  if (js.length > 1) {
    const list = el("ul", "plan");
    for (const x of js) {
      const row = el("li", null, x.when);
      if (["active", "paused"].includes(x.state)) row.append(removeBtn(x));
      list.append(row);
    }
    body.append(list);
  }
  // the next runs — only for a job that fires more often than daily (otherwise the cadence says the same)
  const ups = [...new Set(js.flatMap((x) => x.upcoming || []))].sort((a, b) => a - b);
  if (ups.length > 1 && ups[1] - ups[0] < 82800) body.append(el("p", "runs", runsText(ups.slice(1))));
  if (js.length === 1 && live) {
    if (j.state === "paused") body.append(retarget(j));
    body.append(removeBtn(j));
  }
  d.append(sum, body);
  li.append(d);
  return li;
}

function removeBtn(j) {
  const b = el("button", "btn quiet small remove", t("web.schedule.remove"));
  b.type = "button";
  b.addEventListener("click", () => remove(j));
  return b;
}

// a group's name across snapshots: the core names a group by its earliest job (core/schedule.groups), which changes when
// an earlier run arrives or the first one runs; the row keeps a name its jobs already had. A name goes to one row of a
// drawing: when a group splits, to the half that holds the job the name came from. Jobs that are gone are forgotten
const groupNames = new Map();
export function nameGroups(groups, ids) {
  for (const id of groupNames.keys()) if (!ids.has(id)) groupNames.delete(id);
  const taken = new Set(), names = groups.map(() => null);
  const had = (js) => js.map((x) => groupNames.get(x.id)).filter(Boolean);
  groups.forEach((js, i) => {   // first the halves that hold the job their name came from
    const n = had(js).find((m) => js.some((x) => x.id === m) && !taken.has(m));
    if (n) { names[i] = n; taken.add(n); }
  });
  groups.forEach((js, i) => {   // then the rest: a name it had, an id of its own, and a fresh one when all are taken
    if (names[i]) return;
    names[i] = [...had(js), ...js.map((x) => x.id)].find((m) => !taken.has(m)) || `${js[0].id}#${i}`;
    taken.add(names[i]);
  });
  groups.forEach((js, i) => { for (const x of js) groupNames.set(x.id, names[i]); });
  return names;
}

// the session filter: one field with search (like the sidebar's), no numbers; a chosen session stays in the field,
// one press on the cross shows all sessions again (hierarchy: all → session)
let pickQ = null, pickFor = "";   // text typed into the field (null — not edited), and the session it was typed on
function overview(all, mine) {
  const box = el("div", "sched-overview");
  const names = [...new Set(all.filter((j) => j.state === "active").map(jobOf).filter(Boolean))]
    .sort((a, b) => nameOf(a).localeCompare(nameOf(b), locale));
  if (!names.length && !mine) return box;
  if (pickFor !== (mine || "")) { pickQ = null; pickFor = mine || ""; }   // another screen: the typing is not carried over
  // a snapshot redraws the control: focus keeps its place in it (the field with its caret, the cross, a session walked
  // to with the arrows, the link), and a list closed by Escape stays closed
  const act = document.activeElement, ctl = act?.closest?.(".sched-overview");
  const fk = ctl ? act.dataset.focus : null, shut = !!ctl?.querySelector(".sched-pick-list")?.hidden;
  const sel = fk === "in" ? [act.selectionStart, act.selectionEnd, act.selectionDirection] : null;
  const label = el("label", "search sched-pick");
  const input = el("input", "sched-pick-in");
  input.dataset.focus = "in";
  input.type = "text";
  input.placeholder = t("web.schedule.session");
  input.setAttribute("aria-label", t("web.schedule.session"));
  input.autocomplete = "off";
  input.value = pickQ ?? (mine ? nameOf(mine) : "");   // a snapshot redraws the page: what is being typed stays
  const list = el("ul", "sched-pick-list");
  list.hidden = true;
  // focus leaves the control first (it may be on the picked button): the redraw must not put it back into the list;
  // the session already chosen leaves the hash as it is, so nothing would redraw the half-typed text: draw at once
  const pick = (name) => { pickQ = null; document.activeElement?.blur(); if (name === mine) render(); else location.hash = "#/schedule/" + encodeURIComponent(name); };
  const fill = () => {
    const q = (mine && pickQ === null ? "" : input.value).trim().toLowerCase();
    const hits = names.filter((n) => !q || nameOf(n).toLowerCase().includes(q));
    list.replaceChildren(...(hits.length ? hits.slice(0, 12).map((n) => {
      const li = el("li"), b = el("button", n === mine ? "on" : null, nameOf(n));
      b.type = "button";
      b.dataset.focus = "s:" + n;
      b.addEventListener("mousedown", (e) => e.preventDefault());
      b.addEventListener("click", () => pick(n));
      li.append(b);
      return li;
    }) : [el("li", "none", t("web.schedule.session_none"))]));
    list.hidden = false;
  };
  input.addEventListener("focus", fill);
  const back = () => { input.focus(); list.hidden = true; };   // to the field with the list closed: focus itself opens it
  input.addEventListener("input", () => { pickQ = input.value; clear.hidden = !input.value; fill(); });
  // the list closes when focus leaves the whole control, not when it moves from the field to the cross or to a session (Tab, arrows)
  box.addEventListener("focusout", (e) => { if (!e.relatedTarget || !box.contains(e.relatedTarget)) list.hidden = true; });
  input.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;   // an IME conversion: Enter confirms it, the arrows walk it, Escape cancels it
    if (e.key === "Escape") { list.hidden = true; input.blur(); }
    else if (e.key === "Enter") {   // pick only what was typed: with nothing typed the first of the full list is not the owner's choice
      const bs = list.querySelectorAll("button");
      if (bs.length && ((pickQ ?? "").trim() || bs.length === 1)) bs[0].click();
    } else if (e.key === "ArrowDown") { e.preventDefault(); if (list.hidden) fill(); list.querySelector("button")?.focus(); }
  });
  list.addEventListener("keydown", (e) => {
    const bs = [...list.querySelectorAll("button")], i = bs.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); bs[Math.min(i + 1, bs.length - 1)]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); (i > 0 ? bs[i - 1] : input).focus(); }
    else if (e.key === "Escape") back();
  });
  const clear = el("button", "sched-pick-x");
  clear.dataset.focus = "x";
  clear.type = "button";
  clear.title = t("web.schedule.session_clear");
  clear.setAttribute("aria-label", t("web.schedule.session_clear"));
  clear.append(icon("close"));
  clear.hidden = !input.value;
  clear.addEventListener("click", () => {
    pickQ = null; input.value = ""; clear.hidden = true;
    if (mine) location.hash = "#/schedule"; else back();
  });
  label.append(icon("search"), input, clear);
  box.append(label, list);
  if (mine && (S.snap.sessions || []).some((x) => x.name === mine)) {   // next to the field: the session itself
    const open = el("a", null, t("web.schedule.open_session"));
    open.href = "#/s/" + encodeURIComponent(mine);
    open.dataset.focus = "open";
    box.append(open);
  }
  if (fk) setTimeout(() => {
    if (!shut) fill();   // the sessions exist before one of them takes focus
    const to = [...box.querySelectorAll("[data-focus]")].find((x) => x.dataset.focus === fk && !x.hidden);
    if (!to) { back(); return; }   // the session left the list, the cross went with the text: back to the field
    to.focus();
    if (shut) list.hidden = true;   // the field's focus opens it
    if (sel) try { input.setSelectionRange(...sel); } catch { /* no selection */ }
  }, 0);
  return box;
}

// `hub wait` checks — one-off jobs of a single wait (formerly up to 8, now up to 2) (`<session>-wait-<label>-<n>`):
// one row per wait, hidden by default, a checkbox shows them
const WAITS = "hub.schedule.waits";
// that it is a check is told by the wait field (API 1.25); for older jobs — by the text: the core gives its gist up to
// "Check now" (wait_gist, core/forms.py); a core before it — the English head
const WAIT_HEAD = /^\[waiting for reply\]/;
const waitKey = (j) => (j.wait || j.wait_gist || WAIT_HEAD.test(j.prompt || "")) && (/^(.*-wait-[0-9a-f]+)-\d+$/.exec(j.id) || [])[1];
function foldWaits(all, show) {
  const out = [], series = new Map();
  for (const j of all) {
    const k = waitKey(j);
    if (!k) { out.push(j); continue; }
    const part = `${k}:${["active", "paused"].includes(j.state) ? "live" : "ended"}`;
    series.set(part, [...(series.get(part) || []), j]);
  }
  if (!show) return out;   // hidden: the waits fold away
  for (const js of series.values()) {
    const rep = js.slice().sort((a, b) => (a.next || 9e12) - (b.next || 9e12) || (b.since || 0) - (a.since || 0))[0];
    const n = js.length, gist = rep.wait_gist || (rep.prompt || "").replace(/\. Check now[\s\S]*/, "");
    out.push({ ...rep, prompt: `${gist} · ${t("web.schedule.checks_n", { count: n })}` });
  }
  return out;
}

function waitsToggle(shown) {
  const l = el("label", "sched-waits");
  const box = el("input");
  box.type = "checkbox";
  box.checked = shown;
  box.addEventListener("change", () => { localStorage.setItem(WAITS, box.checked ? "1" : "0"); render(); });
  l.append(box, document.createTextNode(` ${t("web.schedule.waits_label")}`));
  return l;
}

export function scheduleView() {
  const raw = S.snap.schedule || [];
  const totalWaits = raw.filter(waitKey).length;
  const showWaits = localStorage.getItem(WAITS) === "1";
  const all = foldWaits(raw, showWaits);
  const mine = S.scheduleFor;
  const jobs = mine ? all.filter((j) => jobOf(j) === mine) : all;
  const wrap = el("div", "sched");
  const h = el("div", "head");
  const back = el("button", "btn quiet back", t("web.nav.all_sessions"));
  back.type = "button";
  back.addEventListener("click", toList);
  const clock = (S.snap.clock || {}).state === "running" ? null : CLOCK[(S.snap.clock || {}).state] || CLOCK.down;   // the clock runs — silence
  // where you are — in the heading and in the URL: #/schedule is everything, #/schedule/<session> is a session's schedule
  const title = el("h2", "sched-title", t("web.schedule.title"));   // the chosen session is in the "Session" field, not here
  h.append(back, title);
  if (clock) { const pill = el("span", "pill", clock[0]); pill.dataset.s = clock[1]; h.append(pill); }
  const f = el("div", "feed"), inner = el("div", "feed-inner");
  const active = jobs.filter((j) => j.state === "active"), paused = jobs.filter((j) => j.state === "paused");
  const expired = jobs.filter((j) => j.state === "expired");   // the term ran out: renew or remove — a decision
  // finished ones — from newest to oldest: by the time the job finished
  const ended = jobs.filter((j) => !["active", "paused", "expired"].includes(j.state))
    .sort((a, b) => (b.since || b.last_run || 0) - (a.since || a.last_run || 0));
  if (all.length || mine) inner.append(overview(all, mine));
  if (!active.length && !paused.length && !expired.length) {
    const e = el("div", "empty");
    e.append(el("h2", null, mine ? t("web.schedule.none_for", { name: nameOf(mine) }) : t("web.schedule.none")),
      el("p", null, mine ? t("web.schedule.none_hint_session") : t("web.schedule.none_hint")));
    inner.append(e);
  }
  if (paused.length || expired.length) {
    inner.append(el("h3", "day warn", t("web.schedule.needs_decision")));
    const ul = el("ul", "jobs");
    ul.append(...paused.map((j) => jobRow(j)), ...expired.map((j) => jobRow(j)));
    inner.append(ul);
  }
  // groups: under the day of the group's next run, as a single item
  const byGroup = new Map();
  for (const j of active) {
    const k = j.group || j.id;
    if (!byGroup.has(k)) byGroup.set(k, []);
    byGroup.get(k).push(j);
  }
  let day = null, ul = null;
  const groups = [...byGroup.values()], names = nameGroups(groups, new Set(raw.map((j) => j.id)));
  groups.forEach((js, i) => {
    const first = js[0];  // jobs are already ordered by next run time
    const d = first.next ? dayName(first.next) : t("web.schedule.never_again");
    if (d !== day) { day = d; inner.append(el("h3", "day", d)); ul = el("ul", "jobs"); inner.append(ul); }
    ul.append(jobRow(first, js, "group:" + names[i]));
  });
  if (ended.length) {
    const det = el("details", "ended");
    keepOpen(det, "sched:ended");
    det.append(el("summary", null, t("web.schedule.ended", { count: ended.length })));
    const u = el("ul", "jobs");
    u.append(...ended.map((j) => jobRow(j)));
    det.append(u);
    inner.append(det);
  }
  if (totalWaits) inner.append(waitsToggle(showWaits));   // a service switch — at the very end
  f.append(inner);
  wrap.append(h, f);
  return wrap;
}

// the ☰ menu item — the same node between snapshots: one recreated under the mouse used to lose hover (sidebar.js navMenu)
let nav = null;
export function scheduleNav() {
  const jobs = (S.snap.schedule || []).filter((j) => j.state === "active" && j.next);
  const paused = (S.snap.schedule || []).filter((j) => j.state === "paused").length;
  const expired = (S.snap.schedule || []).filter((j) => j.state === "expired").length;
  if (!nav) {
    const b = el("button", "srow nav");
    b.type = "button";
    const g = el("span", "glyph");
    g.append(icon("clock"));
    const txt = el("span", "txt"), nm = el("span", "nm"), lb = el("span", "lb");
    txt.append(nm, lb);
    b.append(g, txt);
    b.addEventListener("click", () => { location.hash = "#/schedule"; });
    nav = { b, g, nm, lb, tag: el("span", "tag"), kit: kit.icons.clock || "" };
  }
  const { b, g, nm, lb, tag } = nav;
  if (nav.kit !== (kit.icons.clock || "")) { nav.kit = kit.icons.clock || ""; g.replaceChildren(icon("clock")); }   // a theme has its own icon
  const cur = String(S.selected === ":schedule");
  if (b.getAttribute("aria-current") !== cur) b.setAttribute("aria-current", cur);
  const next = jobs.length ? Math.min(...jobs.map((j) => j.next)) : null;
  const title = t("web.schedule.title"),
    line = next ? t("web.schedule.nav_next", { jobs: t("web.schedule.jobs_n", { count: jobs.length }), time: hm(next) }) : t("web.schedule.nav_none");
  if (nm.textContent !== title) nm.textContent = title;
  if (lb.textContent !== line) lb.textContent = line;
  if (paused || expired) {
    const p = paused ? t("web.schedule.paused") : t("web.schedule.expired_tag");
    if (tag.textContent !== p) tag.textContent = p;
    if (!tag.isConnected) b.append(tag);
  } else tag.remove();
  return b;
}

