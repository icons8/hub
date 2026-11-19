// Costs and limits. Claude account limits — share of the window used, reset, a forecast from the current pace and the
// price of one percent; session spend for today, 7 days and the limit windows, by day; the most expensive turns.
// Data comes from /api/usage (core/usage.py: cost_usd increments in turn results). A preinstalled plugin — a view of the
// ☰ menu next to "Flow" (#/x/usage); the server is queried only while the view is drawn, at most once a minute.
// A hub with Codex sessions (experiment): Codex gives tokens, not dollars — a hub with both gets a second block, "other",
// of the same form in the other unit; a hub only on Codex — the whole page in tokens (unit).
let hub = null;
const el = (...a) => hub.el(...a);
const POLL_MS = 60000;
const WIN = () => ({ five_hour: hub.t("web.usage.win_five_hour"), seven_day: hub.t("web.usage.win_week"),
  seven_day_opus: hub.t("web.usage.win_week_opus"), seven_day_sonnet: hub.t("web.usage.win_week_sonnet") });
const PERIODS = () => [["today", hub.t("web.usage.period_today")], ["days", hub.t("web.usage.period_days")],
  ["five_hour", hub.t("web.usage.period_five_hour")], ["seven_day", hub.t("web.usage.period_week")]];

// period — per block (the main one and the other one); open — keys "<block>:<session>"
let box = null, data = null, failed = null, period = { main: "today", other: "today" }, open = new Set(), loaded = 0,
  busy = false;

// a sum in the block's unit: dollars, or tokens (Codex gives no money); word — with the unit's name where a bare
// number would read as a price ("1% ≈ 340K tokens")
function money(v, unit, word) {
  if (unit === "tokens") {
    const n = v >= 1e6 ? `${(v / 1e6).toLocaleString(hub.locale, { maximumFractionDigits: 1 })}M`
      : v >= 1e3 ? `${Math.round(v / 1e3).toLocaleString(hub.locale)}K` : `${Math.round(v || 0)}`;
    return word ? hub.t("web.usage.cx_f_tokens", { n }) : n;
  }
  if (!v) return "$0";
  if (v >= 100) return `$${Math.round(v).toLocaleString(hub.locale)}`;
  const d = v >= 10 ? 1 : 2;
  return `$${v.toLocaleString(hub.locale, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
}
const pct = (v) => `${Math.round(v * 100)}%`;
// a window's name: Claude's and Codex's 5 h and week by name; another Codex window (codex_<N>m) by its length — the
// words of the limits line (static/js/limits.js codexWin)
function winName(name) {
  if (WIN()[name]) return WIN()[name];
  const m = /^codex_(\d+)m$/.exec(name);
  if (!m) return name;
  const mins = Number(m[1]);
  return mins % 1440 === 0 ? hub.t("web.limits.cx_f_days", { n: mins / 1440 })
    : mins % 60 === 0 ? hub.t("web.limits.cx_f_hours", { n: mins / 60 }) : hub.t("web.limits.cx_f_minutes", { n: mins });
}
function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString(hub.locale, { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return hub.t("web.usage.at_time", { time: hm });
  return d.toLocaleDateString(hub.locale, { weekday: "short", day: "numeric", month: "short" }) + `, ${hm}`;
}
function dayName(iso, i, n) {
  if (i === n - 1) return hub.t("web.usage.today");
  if (i === n - 2) return hub.t("web.usage.yesterday");
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString(hub.locale, { weekday: "short", day: "numeric" });
}

async function load() {
  if (busy) return;
  busy = true;
  loaded = Date.now();
  try {
    const r = await fetch("/api/usage");
    if (!r.ok) {   // 503 — journal archives are still being read: the refusal text says when to ask again
      const said = await r.json().then(j => j && j.error, () => null);
      throw new Error(said || hub.t("web.usage.server_answered", { status: r.status }));
    }
    data = await r.json();
    failed = null;
  } catch (e) {
    failed = e.message || String(e);
  }
  busy = false;
  paint();
}

// a limit window: share used, reset, forecast and what one percent costs
function windowCard(w, unit) {
  const used = Math.round(w.used * 100);
  const card = el("section", "us-win");
  card.dataset.level = used >= 90 ? "high" : used >= 75 ? "warn" : "ok";
  const head = el("div", "us-win-head");
  head.append(el("span", "us-win-name", winName(w.name)), el("span", w.gone ? "us-win-pct us-muted" : "us-win-pct", w.gone ? "—" : `${used}%`));   // the reset has passed: not 0%
  const bar = el("div", "us-bar");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", hub.t("web.usage.aria_used", { name: winName(w.name), used }));
  const fill = el("span");
  fill.style.width = `${Math.min(used, 100)}%`;
  bar.append(fill);
  if (w.at_reset != null && w.at_reset > w.used) {   // where it will get by the reset at the current pace — a tick on top
    const mark = el("span", "us-bar-proj");
    mark.style.insetInlineStart = `${Math.min(w.at_reset * 100, 100)}%`;   // the bar fills from the line start
    bar.append(mark);
  }
  card.append(head);
  if (!w.gone) card.append(bar);   // the reset has passed: no empty bar "0% used" - the line below says it
  const lines = el("div", "us-win-lines");
  if (w.gone) lines.append(el("div", null, hub.t("web.accounts.window_reset")));   // the reset has passed: no fresh reading
  else if (w.resets) lines.append(el("div", null, hub.t("web.usage.reset", { when: when(w.resets) })));
  if (w.eta && w.resets && w.eta < w.resets) {
    lines.append(el("div", "us-warn", hub.t("web.usage.runs_out", { when: when(w.eta) })));
  } else if (w.at_reset != null) {
    lines.append(el("div", null, hub.t("web.usage.pace", {
      rate: w.rate_h.toLocaleString(hub.locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
      share: pct(Math.min(w.at_reset, 1)) })));
  } else if (w.used > 0) {
    lines.append(el("div", "us-muted", hub.t("web.usage.no_pace")));
  }
  const cost = [hub.t("web.usage.since_start", { cost: money(w.total, unit, true) })];
  if (w.per_pct) cost.push(hub.t("web.usage.per_pct", { cost: money(w.per_pct, unit, true) }));
  if (!w.gone) lines.append(el("div", "us-muted", cost.join(" · ")));   // after the reset the window's spend is unknown, not $0
  card.append(lines);
  return card;
}

// spend by day as bars; a click shows that day in the sessions table. v — the block: the report or its other part;
// key — its name in period and open
function daysChart(v, key) {
  const days = v.days, unit = v.unit;
  const wrap = el("section", "us-days");
  wrap.append(el("h3", "us-h", hub.t("web.usage.by_day")));
  const max = Math.max(...days.map((d) => d.total), 0.01);
  const row = el("div", "us-cols");
  days.forEach((d, i) => {
    const b = el("button", "us-col");
    b.type = "button";
    b.title = `${d.day}: ${money(d.total, unit, true)}`;
    b.setAttribute("aria-label", `${dayName(d.day, i, days.length)}: ${money(d.total, unit, true)}`);
    if (period[key] === `day:${d.day}` || (period[key] === "today" && i === days.length - 1)) b.classList.add("on");
    const val = el("span", "us-col-v", d.total ? money(d.total, unit) : "");
    const track = el("span", "us-col-track");
    const f = el("span", "us-col-fill");
    f.style.height = `${Math.max(d.total ? 3 : 0, (d.total / max) * 100)}%`;
    track.append(f);
    b.append(val, track, el("span", "us-col-day", dayName(d.day, i, days.length)));
    b.addEventListener("click", () => { period[key] = i === days.length - 1 ? "today" : `day:${d.day}`; paint(); });
    row.append(b);
  });
  wrap.append(row);
  return wrap;
}

function spentBy(v, p) {
  if (p === "today") return v.days[v.days.length - 1].by;
  if (p.startsWith("day:")) return (v.days.find((d) => d.day === p.slice(4)) || { by: {} }).by;
  if (p === "days") {
    const by = {};
    for (const d of v.days) for (const [k, n] of Object.entries(d.by)) by[k] = (by[k] || 0) + n;
    return by;
  }
  return (v.windows.find((w) => w.name === p) || { by: {} }).by;
}

// executors (and forks from old journals) go under the session that started them (up the chain to the root)
function rootOf(name) {
  const seen = new Set();
  while (data.parents[name] && !seen.has(name)) { seen.add(name); name = data.parents[name]; }
  return name;
}

// a model call made by a core module (module.model) is an item mod:<name>: labelled "module <name>", with no session link;
// the core's own Codex model (the watcher and others, experiment) — codex:<who>, the same way
const MOD = "mod:", CORE = "codex:";
function nameLink(name, label) {
  if (name.startsWith(CORE)) return el("span", "us-name", hub.t("web.usage.cx_f_core", { name: name.slice(CORE.length) }));
  if (!name.startsWith(MOD)) {
    const a = el("a", "us-name", label || name);
    a.href = "#/s/" + encodeURIComponent(name);
    return a;
  }
  return el("span", "us-name", hub.t("web.usage.module", { name: name.slice(MOD.length) }));
}

function sessionsTable(v, key) {
  const unit = v.unit, p = period[key];
  const wrap = el("section", "us-sess");
  const head = el("div", "us-sess-head");
  head.append(el("h3", "us-h", hub.t("web.usage.sessions")));
  const tabs = el("div", "us-tabs");
  tabs.setAttribute("role", "tablist");
  const have = new Set(["today", "days", ...v.windows.map((w) => w.name)]);
  // the windows of the block in order: Claude's and Codex's 5 h and week have their tab names, another Codex window — its name
  const tabsOf = [...PERIODS(), ...v.windows.filter((w) => !PERIODS().some(([id]) => id === w.name))
    .map((w) => [w.name, winName(w.name)])];
  for (const [id, label] of tabsOf) {
    if (!have.has(id)) continue;
    const t = el("button", "us-tab", label);
    t.type = "button";
    t.setAttribute("role", "tab");
    t.setAttribute("aria-selected", String(p === id));
    t.addEventListener("click", () => { period[key] = id; paint(); });
    tabs.append(t);
  }
  if (p.startsWith("day:")) {
    const d = v.days.findIndex((x) => x.day === p.slice(4));
    const t = el("button", "us-tab", dayName(p.slice(4), d, v.days.length));
    t.type = "button";
    t.setAttribute("role", "tab");
    t.setAttribute("aria-selected", "true");
    tabs.append(t);
  }
  head.append(tabs);
  wrap.append(head);
  const groups = new Map();
  for (const [name, n] of Object.entries(spentBy(v, p))) {
    const r = rootOf(name);
    const g = groups.get(r) || { name: r, total: 0, own: 0, kids: [] };
    g.total += n;
    if (r === name) g.own += n; else g.kids.push([name, n]);
    groups.set(r, g);
  }
  const list = [...groups.values()].sort((a, b) => b.total - a.total);
  const sum = list.reduce((s, g) => s + g.total, 0);
  if (!list.length) {
    wrap.append(el("p", "us-empty", hub.t("web.usage.no_spend")));
    return wrap;
  }
  const table = el("div", "us-rows");
  const max = list[0].total || 1;
  for (const g of list) {
    const row = el("div", "us-row");
    const name = nameLink(g.name);
    const main = el("div", "us-main");
    main.append(name);
    const id = `${key}:${g.name}`;
    if (g.kids.length) {
      const more = el("button", "us-kids", `${open.has(id) ? "▾" : "▸"} ${hub.t("web.usage.executors", { count: g.kids.length })}`);
      more.type = "button";
      more.setAttribute("aria-expanded", String(open.has(id)));
      more.addEventListener("click", () => { open.has(id) ? open.delete(id) : open.add(id); paint(); });
      main.append(more);
    }
    const bar = el("span", "us-rbar");
    const f = el("span");
    f.style.width = `${(g.total / max) * 100}%`;
    bar.append(f);
    row.append(main, bar, el("span", "us-share", sum ? `${Math.round((g.total / sum) * 100)}%` : ""),
      el("span", "us-usd", money(g.total, unit)));
    table.append(row);
    if (open.has(id)) {
      for (const [k, n] of [[g.name, g.own], ...g.kids.sort((a, b) => b[1] - a[1])]) {
        const sub = el("div", "us-row us-sub");
        sub.append(nameLink(k, k === g.name ? hub.t("web.usage.self", { name: k }) : k), el("span"), el("span", "us-share", sum ? `${Math.round((n / sum) * 100)}%` : ""),
          el("span", "us-usd", money(n, unit)));
        table.append(sub);
      }
    }
  }
  wrap.append(table, el("div", "us-total", hub.t("web.usage.total", { sum: money(sum, unit, true) })));
  const unpriced = Object.entries(data.unpriced || {}).filter(([k]) => k.startsWith(MOD));
  if (unpriced.length && key === "main") {   // calls without a price (timed out) are not zero: shown as a count, for 7 days
    wrap.append(el("p", "us-note us-unpriced", hub.t("web.usage.unpriced", {
      list: unpriced.map(([k, n]) => `${k.slice(MOD.length)} — ${n}`).join(", ") })));
  }
  return wrap;
}

// notable items — the most expensive turns of the last 7 days: who, when, what was done
function topTurns(v) {
  const wrap = el("section", "us-top");
  wrap.append(el("h3", "us-h", hub.t("web.usage.top_title")));
  if (!v.top.length) { wrap.append(el("p", "us-empty", hub.t("web.usage.top_empty"))); return wrap; }
  const list = el("ol", "us-top-list");
  for (const t of v.top) {
    const li = el("li", "us-turn");
    const a = el("a", "us-name", t.session);
    a.href = "#/s/" + encodeURIComponent(t.session);
    const meta = el("div", "us-turn-meta");
    const mins = t.ms ? Math.max(1, Math.round(t.ms / 60000)) : null;
    meta.append(a, el("span", "us-muted", when(t.ts) + (mins ? ` · ${hub.t("web.usage.minutes", { mins })}` : "")), el("span", "us-usd", money(t.usd, v.unit)));
    const text = (t.text || "").split("\n").find((s) => s.trim()) || hub.t("web.usage.no_text");
    li.append(meta, el("div", "us-turn-text", text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[*_`#>]/g, "")));
    list.append(li);
  }
  wrap.append(list);
  return wrap;
}

// one block of the page: limit windows, by day, sessions, the costliest turns — in the block's unit
function block(page, v, key) {
  if (v.status === "rejected") page.append(el("p", "us-stop", hub.t("web.usage.limit_hit")));
  if (v.windows.length) {
    const wins = el("div", "us-wins");
    for (const w of v.windows) wins.append(windowCard(w, v.unit));
    page.append(wins);
  } else {
    page.append(el("p", "us-empty", hub.t("web.usage.no_limits")));
  }
  page.append(daysChart(v, key), sessionsTable(v, key), topTurns(v));
}

function paint() {
  if (!box) return;
  const page = el("div", "us");
  const head = el("header", "us-head");
  head.append(el("h2", "us-title", hub.t("web.usage.title")), el("p", "us-lead", hub.t("web.usage.hint")));
  page.append(head);
  if (!data) {
    page.append(el("p", "us-empty", failed ? hub.t("web.usage.load_failed", { error: failed }) : hub.t("web.usage.loading")));
    box.replaceChildren(page);
    return;
  }
  // a hub with Claude and Codex: each its own block under its heading — dollars and tokens never go into one sum
  const title = (v) => el("h2", "us-h us-codex-h", hub.t(v.unit === "tokens" ? "web.usage.cx_f_codex_title"
    : "web.usage.cx_f_claude_title"));
  if (data.other) page.append(title(data));
  block(page, data, "main");
  if (data.other) {
    page.append(title(data.other));
    block(page, data.other, "other");
  }
  for (const v of [data, data.other].filter(Boolean)) {
    page.append(el("p", "us-note", hub.t(v.unit === "tokens" ? "web.usage.cx_f_note_tokens" : "web.usage.note")));
  }
  if (failed) page.append(el("p", "us-warn", hub.t("web.usage.refresh_failed", { error: failed })));
  box.replaceChildren(page);
}

export default function register(h) {
  hub = h;
  hub.addStyle("usage.css");
  hub.addView({
    id: "usage", title: hub.t("web.usage.title"), subtitle: hub.t("web.usage.hint"), icon: "usage",
    render(root) {
      if (box !== root) { box = root; paint(); }
      if (Date.now() - loaded > POLL_MS) load();
    },
  });
}
