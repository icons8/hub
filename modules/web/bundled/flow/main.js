// Flow: who writes to whom — dots and the signals between them, an animation of the journal as animated dots and
// signals. You and the hub are in the center, agents form the first ring, the other sessions the second. A signal is
// a comet along an arc; a dot flashes when the signal reaches it.
// "Now" is the live flow (/api/flow); "1 h / 6 h / day" replays the journal over a minute (/api/flow/history).
// A hub plugin preinstalled by default, plugin API 1.1: a view with show/hide — the canvas draws and polls the core
// only while it is visible.
let hub = null;
const el = (...a) => hub.el(...a);
const S = { get snap() { return hub.snapshot() || {}; } };

const KIND = { message: "--flow-msg", task: "--flow-task", result: "--flow-result" };
// the words on the lines are in the canvas language, like the dot names next to them (tx, not hub.t)
const KIND_NAME = () => ({ message: tx("web.flow.kind_message"), task: tx("web.flow.kind_task"), result: tx("web.flow.kind_result") });

// Canvas language: whoever embeds Flow (hubFlow.setLang) may name its own language — dot labels and the tooltip use it,
// and the strings are the same strings as the core page's in that language (/i18n.json?lang=). If none is named or it
// is the same language, the page language is used.
let appText = null, langGen = 0;
function tx(key, params = {}) {
  if (!appText) return hub.t(key, params);
  let v = appText.messages[key], r = appText.rules;
  if (v == null) { v = appText.fallback[key]; r = appText.en; }
  if (v == null) return hub.t(key, params);
  if (typeof v === "object") v = v[r.select(params.count ?? 0)] ?? v.other ?? key;
  return String(v).replace(/\{\{|\}\}|\{(\w+)\}/g, (m, k) => (!k ? m[0] : k in params ? String(params[k]) : m));   // like fill in js/i18n.js
}
const langBase = (x) => String(x || "").toLowerCase().split("-")[0];
async function setLang(code) {
  if (!hub) return;
  const want = String(code || ""), n = ++langGen;
  let next = null;
  if (want && langBase(want) !== langBase(hub.lang)) {
    try {
      const r = await fetch(`/i18n.json?lang=${encodeURIComponent(want)}`, { credentials: "same-origin" });
      const d = await r.json();
      if (!r.ok || typeof d.lang !== "string" || !d.messages) return;   // no strings — labels stay as they were
      if (d.lang !== hub.lang) next = { lang: d.lang, messages: d.messages || {}, fallback: d.fallback || {},
        rules: new Intl.PluralRules(d.lang === "xx" ? "en" : d.lang), en: new Intl.PluralRules("en") };
    } catch { return; }   // no strings — labels stay in the page language
  }
  if (n !== langGen) return;   // another language was named while we waited
  appText = next; widths.clear();
  if (hover || selected) showTip(hover || selected);
  notify();
}
const REPLAY_MS = 60000;   // a replay of any window takes a minute
const FLY_MS = 1100;       // a signal's flight
const LIVE_LINE = 0.16;     // the least opacity of a line in "Now" while its signal is in the window
const EDGE_HOLD = 600;     // seconds on the flow scale that a link's trail lasts (at least 3% of the replay window)
const holdSpan = () => replay ? Math.max(EDGE_HOLD, (replay.to - replay.from) * 0.03) : EDGE_HOLD;
const SEEK_FLY = 8;        // after a seek — how many of the last signals before the point fly again

let root = null, canvas, cx2d, raf = 0, poll = 0, last = 0, mode = "live", replay = null, gen = 0;   // gen — the number of the mode switch-on
let loading = false, failed = false;   // the replay window is loading / failed to load
let nodes = new Map(), pulses = [], ripples = [], edges = new Map(), hover = null, signals = 0, clockEl, bar, noteEl;
let colors = {}, dpr = 1, W = 0, H = 0;
// replay at different speeds, pause and seek: ×1 — the window over REPLAY_MS
const SPEEDS = [["×¼", 0.25], ["×½", 0.5], ["×1", 1], ["×2", 2], ["×4", 4]];
let speed = 1, seekEl = null, speedEl = null, periodSelect = null, speedSelect = null, seeking = false;
// animation time: normally the page time; when recording a demo — frame by frame (__flowRec), exactly 60 frames per second
let fakeNow = null, labelPx = 12;
const clock = () => fakeNow ?? performance.now();

function css(name, fallback = "#888") { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; }
function readColors() {
  colors = { fg: css("--foreground"), muted: css("--muted-foreground"), border: css("--border"), bg: css("--background"),
    owner: css("--warning"), hub: css("--primary"), agent: css("--success"), core: css("--muted-foreground") };
  // Flow's variables are declared in flow.css: it may not have loaded yet — then the same colors as before them (not grey #888)
  colors.worker = css("--flow-quiet", colors.muted); colors.label = css("--flow-label", colors.muted);
  labelPx = Math.max(8, Math.min(32, parseFloat(css("--flow-label-size")) || 12));
  for (const [k, v] of Object.entries(KIND)) colors[k] = css(v);
}

// a big hub (with ~380 sessions labels and icons merged into a mess): with more than BIG live sessions we draw only
// those with events in the window ("Now" — the last LIVE_WINDOW seconds, or working with executors), plus "You", "Hub", "core",
// "schedule"; a lead session's executors are folded under it (a dashed ring; a click on the lead shows them); no more than MAX_DRAWN
// dots, the rest as a number. A small hub (up to BIG sessions) draws all dots and labels, as before.
const BIG = 30, MAX_DRAWN = 48, LIVE_WINDOW = 900, DEAD = ["closed", "failed", "removed"];
let touched = new Map();   // session -> { at: when the last signal came, n: how many signals } in the current window
let expanded = new Set();  // lead sessions whose executors are expanded
let cur = { big: false, draw: new Set(), fold: new Map(), folded: new Map(), cut: new Set(), stale: new Set(), total: 0, shown: 0 };
let memo = { list: null, by: new Map() }, lastLabels = [], lastKinds = [], selected = null;
function sess() {   // snapshot sessions by name: no list search for every node and frame
  const list = S.snap.sessions || [];
  if (memo.list !== list) memo = { list, by: new Map(list.map((x) => [x.name, x])) };
  return memo.by;
}
function note(f) {   // a signal counts as activity of both ends in the window
  for (const id of [f.from, f.to]) {
    if (SPECIAL.has(id)) continue;
    const a = touched.get(id) || { at: 0, n: 0 };
    a.at = Math.max(a.at, f.ts); a.n += 1;
    touched.set(id, a);
  }
}

// the liaison is named as in the session list (in the page language), the rest as they are
const label = (id) => id === "owner" ? tx("web.flow.you") : id === "core" ? tx("web.flow.core") : id === "schedule" ? tx("web.flow.schedule")
  : id === "liaison" && sess().get(id)?.system ? tx("web.sidebar.liaison") : id;

function roleOf(id, byName) {
  if (id === "owner") return "owner";
  if (id === "core" || id === "schedule") return "core";
  const s = byName.get(id);
  if (cur.big && cur.fold.has(id)) return "exec";   // an expanded executor is a satellite of its lead
  if (!s) return "worker";
  if (s.kind === "hub") return "hub";
  return s.role === "agent" ? "agent" : "worker";
}

function ensure(id) {
  let n = nodes.get(id), made = false;
  if (!n) {
    // placed — the node is already in its place: a new one is put there by layout, rather than flying from the center
    n = { id, x: W / 2, y: H / 2, tx: W / 2, ty: H / 2, heat: 0, out: 0, in: 0, placed: false };
    nodes.set(id, n);
    made = true;
  }
  return [n, made];
}
function node(id) {
  const [n, made] = ensure(id);
  if (made) layout();
  return n;
}

// a session was deleted — from that moment it is gone from Flow: its dot, its links and the signals flying to it
const SPECIAL = new Set(["owner", "core", "schedule"]);
function gone(id) {
  if (SPECIAL.has(id) || !nodes.has(id)) return;
  if (selected === nodes.get(id)) selected = null;
  const n = nodes.get(id);
  nodes.delete(id);
  for (const [k, e] of edges) if (e.a === n || e.b === n) edges.delete(k);
  pulses = pulses.filter((p) => p.a !== n && p.b !== n);
  ripples = ripples.filter((r) => r.n !== n);
  if (hover === n) hover = null;
}
const alive = () => new Set((S.snap.sessions || []).map((s) => s.name));
const liveOk = (f, names) => [f.from, f.to].every((x) => SPECIAL.has(x) || names.has(x));

// whom we draw (big hub): names with events in the window and those working now; a lead session (raised_by) folds its
// executors when the lead itself is on the canvas; beyond MAX_DRAWN we cut off the least active and count them
function plan() {
  const by = sess(), list = [...by.values()].filter((x) => !DEAD.includes(x.state));
  const p = { big: list.length > BIG, draw: new Set(), fold: new Map(), folded: new Map(), cut: new Set(), stale: new Set(),
    total: list.length, shown: 0 };
  if (!p.big) return p;
  const live = mode === "live", since = Date.now() / 1000 - LIVE_WINDOW, act = new Map();
  for (const [id, a] of touched) {
    const x = by.get(id);
    if (live && (a.at < since || !x || DEAD.includes(x.state))) { p.stale.add(id); continue; }   // outside the window — not drawn
    act.set(id, { n: a.n, at: a.at, busy: false });
  }
  const hubName = (list.find((x) => x.kind === "hub") || {}).name;
  if (live) for (const x of list) if (x.state === "busy" || (x.pending || []).length) act.set(x.name, { ...(act.get(x.name) || { n: 0, at: 0 }), busy: true });
  if (hubName) act.delete(hubName);   // the hub is always drawn, not counted and not cut
  // the lead session is the top of the chain of active ones — up to the hub (sessions raised by the hub lead
  // themselves), at any depth: otherwise an executor would land on a lead that is itself folded and not on the canvas
  const lead = (id) => {
    let c = (by.get(id) || {}).raised_by, top = null;
    const seen = new Set([id]);
    while (c && c !== hubName && act.has(c)) {
      if (seen.has(c)) return null;   // a loop in raised_by — do not fold
      seen.add(c); top = c; c = (by.get(c) || {}).raised_by;
    }
    return top;
  };
  let roots = [];
  for (const id of act.keys()) { const l = lead(id); if (l) p.fold.set(id, l); else roots.push(id); }
  roots.sort((x, y) => { const a = act.get(x), b = act.get(y); return (b.busy - a.busy) || (b.n - a.n) || (b.at - a.at); });
  for (const id of roots.slice(MAX_DRAWN)) p.cut.add(id);
  roots = roots.slice(0, MAX_DRAWN);
  for (const [id, l] of [...p.fold]) {
    if (p.cut.has(l)) { p.fold.delete(id); p.cut.add(id); continue; }
    p.folded.set(l, [...(p.folded.get(l) || []), id]);
  }
  for (const id of roots) p.draw.add(id);
  if (hubName) p.draw.add(hubName);
  for (const l of expanded) for (const id of p.folded.get(l) || []) p.draw.add(id);   // expanded ones — dots next to their lead
  p.shown = roots.length + p.fold.size + (hubName ? 1 : 0);   // the hub is also a session of total
  return p;
}

const known = (id) => !cur.big || SPECIAL.has(id) || cur.draw.has(id) || cur.fold.has(id) || cur.cut.has(id) || cur.stale.has(id);
// the node a session's signal lands on: the session itself, its lead (the executor is folded) or null (not shown)
// "Now" on a small hub: a session that is not working and had no signal in the window has no line. It stays on the
// canvas as a quiet dot — an empty ring of the dot's size, with no name until it is pointed at. A session at work or
// with a question is never quiet (it breathes and is named), nor are the hub and the special dots. A replay has none
function quiet(id, by = sess()) {
  const x = by.get(id) || {};
  if (mode !== "live" || SPECIAL.has(id) || x.kind === "hub" || x.state === "busy" || (x.pending || []).length) return false;
  const a = touched.get(id);
  return !a || a.at < Date.now() / 1000 - LIVE_WINDOW;
}
function resolve(id) {
  if (SPECIAL.has(id)) return node(id);
  if (!cur.big) return node(id);
  if (!known(id)) layout();   // new activity — the window changed
  if (cur.draw.has(id)) return node(id);
  const l = cur.fold.get(id);
  return l && cur.draw.has(l) ? node(l) : null;
}

// layout: the hub in the center with you above it; rings hold agents, other sessions, foreign hubs
// the one line under the bar: how many sessions the canvas left out (the drawing cap); a hub that fits has no line
function showNote() {
  if (!noteEl) return;
  noteEl.hidden = !(cur.big && cur.cut.size);
  noteEl.textContent = noteEl.hidden ? "" : hub.t("web.flow.note_hidden", { n: cur.cut.size });
}

const WORKER_RINGS = [0.72, 0.88, 1.04];   // big hub: many executors and sessions — several rings, not one circle
// the drawing fills the canvas: the rings are stretched from the center, each half on its own, until the outermost dot
// is a margin of 48px (plus room for the names at the sides and under the lowest dot) from the edge. "You" and "Hub" stay
// in the center — a dot that comes or goes does not move them; a half is not blown up more than three times
const FIT_MARGIN = 48, FIT_MAX = 3;
function fit(list) {
  if (!(W > 0 && H > 0) || !list.length) return;
  const side = FIT_MARGIN + Math.min(72, W * 0.1), top = FIT_MARGIN + 4, bottom = FIT_MARGIN + 28;
  const half = (key, c, edge, sign) => {
    const far = Math.max(...list.map((n) => (n[key] - c) * sign));
    if (far < 1 || (edge - c) * sign <= 0) return;
    const k = Math.min(FIT_MAX, (edge - c) * sign / far);
    for (const n of list) if ((n[key] - c) * sign > 0) n[key] = c + (n[key] - c) * k;
  };
  half("tx", W / 2, side, -1); half("tx", W / 2, W - side, 1);
  half("ty", H / 2, top, -1); half("ty", H / 2, H - bottom, 1);
}

function layout() {
  if (mode === "live") { const names = alive(); for (const id of [...nodes.keys()]) if (!names.has(id)) gone(id); }
  cur = plan();
  showNote();
  laidFor = S.snap.sessions || null;
  if (cur.big) {
    for (const id of [...nodes.keys()]) if (!SPECIAL.has(id) && !cur.draw.has(id)) gone(id);   // left the window — no dot
    for (const id of cur.draw) ensure(id);
    ensure("core"); ensure("schedule");
  } else {
    for (const s of S.snap.sessions || []) {
      if (DEAD.includes(s.state)) continue;
      ensure(s.name);
    }
  }
  ensure("owner");
  const cx = W / 2, cy = H / 2, ry = Math.max(120, H / 2 - 70), rx = Math.max(160, Math.min(W / 2 - 90, ry * 1.6));
  const groups = { agent: [], worker: [], core: [], exec: [] };
  const byName = sess();
  for (const n of nodes.values()) {
    const r = roleOf(n.id, byName);
    n.role = r;
    // you and the hub are a pair in the center: you on the left, the hub on the right
    if (r === "hub") { n.tx = cx + rx * 0.12; n.ty = cy; } else if (r === "owner") { n.tx = cx - rx * 0.12; n.ty = cy; }
    else groups[r].push(n);
  }
  const ring = (list, k, phase) => {
    list.sort((a, b) => a.id.localeCompare(b.id));
    list.forEach((n, i) => {
      const a = phase + (i / Math.max(1, list.length)) * Math.PI * 2;
      n.tx = cx + Math.cos(a) * rx * k; n.ty = cy + Math.sin(a) * ry * k;
    });
  };
  ring(groups.agent, 0.45, -Math.PI / 2 + 0.3);
  if (cur.big && groups.worker.length > 16) {   // the inner ring is shorter, so proportionally fewer dots go into it
    groups.worker.sort((a, b) => (a.id < b.id ? -1 : 1));
    const sum = WORKER_RINGS.reduce((x, y) => x + y, 0), left = [...groups.worker];
    WORKER_RINGS.forEach((k, i) => {
      const part = i === WORKER_RINGS.length - 1 ? left.splice(0) : left.splice(0, Math.round(groups.worker.length * k / sum));
      ring(part, k, -Math.PI / 2 + 0.1 + i * 0.37);
    });
  } else ring(groups.worker, 0.8, -Math.PI / 2 + 0.1);
  groups.core.forEach((n, i) => { n.tx = cx + (i ? 1 : -1) * rx * (cur.big ? 0.3 : 0.12); n.ty = cy + ry * 0.2; });
  fit([...groups.agent, ...groups.worker, ...groups.core]);
  // expanded executors stand around their lead in cells of a grid: a cell is a dot with its name under it, as wide as
  // the name cut to one length (shorter cuts, down to 8 letters, keep the cells near the lead when there are many), so
  // every name stands under its dot and no name lies on a dot. A cell never takes the place of another dot or its name
  // and stays on the canvas; the nearest free cells to the lead are taken first
  const CELL = { up: 10, down: 30, gap: 6 }, near = Math.min(W, H) * 0.3;
  const wideOf = (id, letters) => (Math.min(letters, label(id).length) + 1) * 7 + 12;
  const blocks = [...nodes.values()].filter((n) => n.role !== "exec").map((n) => {
    const w = wideOf(n.id, 22) + (foldedOf(n).length ? 28 : 0);
    return { x0: n.tx - w / 2, x1: n.tx + w / 2, y0: n.ty - CELL.up, y1: n.ty + CELL.down };
  });
  const hit = (a) => blocks.some((b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1);
  const cellsOf = new Map();
  const cells = (l) => {
    if (cellsOf.has(l)) return cellsOf.get(l);
    const all = cur.folded.get(l.id) || [];
    const longest = Math.max(8, ...all.map((id) => Math.min(22, label(id).length)));
    let best = null;
    // the grid goes through the lead; when that leaves too few cells (a narrow canvas loses a whole column to the
    // lead's place by its edge), it is laid evenly over the canvas instead
    const even = (at, size, span) => { const k = Math.floor((span - 8) / size), first = 4 + size / 2 + (span - 8 - k * size) / 2;
      return ((first - at) % size + size) % size; };
    const scan = (cw, ch, dx, dy) => {
      const free = [], ni = Math.ceil(W / cw) + 1, nj = Math.ceil(H / ch) + 1;
      for (let i = -ni; i <= ni; i++) for (let j = -nj; j <= nj; j++) {
        const x = l.tx + dx + i * cw, y = l.ty + dy + j * ch, c = { x0: x - cw / 2, x1: x + cw / 2, y0: y - CELL.up, y1: y + CELL.down };
        if ((i || j || dx || dy) && c.x0 >= 4 && c.x1 <= W - 4 && c.y0 >= 4 && c.y1 <= H - 4 && !hit(c))
          free.push({ x, y, c, d: Math.hypot(x - l.tx, y - l.ty) });
      }
      free.sort((a, b) => a.d - b.d);
      const got = free.slice(0, all.length);
      return Object.assign(got, { far: got.length ? got[got.length - 1].d : 0 });
    };
    for (let letters = longest; ; letters -= 2) {
      const cw = Math.max(...all.map((id) => wideOf(id, Math.max(8, letters)))) + CELL.gap, ch = CELL.up + CELL.down;
      let got = scan(cw, ch, 0, 0);
      if (got.length < all.length) {
        const mid = (CELL.down - CELL.up) / 2;   // a cell reaches further below its dot than above it
        const spread = scan(cw, ch, even(l.tx, cw, W), even(l.ty + mid, ch, H));
        if (spread.length > got.length) got = spread;
      }
      got.sort((a, b) => a.y - b.y || a.x - b.x);   // the names read in rows, in their order
      if (!best || got.length > best.length || (got.length === best.length && got.far < best.far)) best = got;
      if ((got.length === all.length && got.far <= near) || letters <= 8) break;
    }
    best.forEach((p) => blocks.push(p.c));
    cellsOf.set(l, best);
    return best;
  };
  const around = new Map();
  for (const n of groups.exec.sort((a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }))) {
    const l = nodes.get(cur.fold.get(n.id)), i = around.get(l) || 0, got = cells(l);
    around.set(l, i + 1);
    // no free cell left (a canvas too small for them all): next to the lead, inside the canvas
    const at = got[i] || { x: Math.min(W - 24, Math.max(24, l.tx + 24 * (i % 2 ? -1 : 1))), y: Math.min(H - 36, Math.max(20, l.ty + 36)) };
    n.tx = at.x; n.ty = at.y;
  }
  // a new node goes straight into place as soon as there is a canvas: by a flag, not by the node's age (on a big hub
  // layout itself took 130–270 ms, so an "younger than 50 ms" check after it never fired — "You" and "Hub" were born
  // in the center)
  if (W > 0 && H > 0) for (const n of nodes.values()) if (!n.placed) { n.x = n.tx; n.y = n.ty; n.placed = true; }
}

// the canvas size did not change — no canvas redraw and no layout: the first ResizeObserver call right after flowStart
// arrives with the same size, and the layout of a big hub takes hundreds of ms. false — nothing was done
function resize() {
  if (!canvas) return false;
  const r = canvas.getBoundingClientRect(), d = window.devicePixelRatio || 1;
  if (r.width === W && r.height === H && d === dpr) return false;
  dpr = d;
  W = r.width; H = r.height;
  canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  cx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
  layout();
  return true;
}

// a signal: a comet from from to to; t — the signal's time on the flow scale (for the link trail)
function fire(f, delay = 0) {
  const ab = mark(f);
  if (!ab) return;
  const [a, b] = ab;
  if (a === b) { ripples.push({ n: a, kind: f.kind, start: clock() + delay }); a.heat = Math.min(3, a.heat + 0.5); return; }   // executor ↔ its lead
  pulses.push({ a, b, kind: f.kind, start: clock() + delay, bend: (Math.random() - 0.5) * 0.5 });
}

// a link trail and the signal count without a flight (seek; fire — with a flight)
function mark(f) {
  const a = resolve(f.from), b = resolve(f.to);
  signals += 1;   // counts all signals, including those whose dots are absent (hubFlow.signals(); not on screen)
  if (!a || !b) return null;
  a.out += 1;
  if (a === b) return [a, b];
  edgeAdd(a, b, f);
  return [a, b];
}
function edgeAdd(a, b, f) {
  const key = a.id + "→" + b.id;
  const e = edges.get(key) || { a, b, n: 0, at: 0 };
  e.n += 1; e.at = f.ts; e.mod = !!f.mod; e.kind = f.kind; edges.set(key, e);   // a signal from a module's write with core.write is drawn dashed
}

function bez(p, a, b, bend) {
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dx = b.x - a.x, dy = b.y - a.y;
  const qx = mx - dy * bend, qy = my + dx * bend, u = 1 - p;
  return [u * u * a.x + 2 * u * p * qx + p * p * b.x, u * u * a.y + 2 * u * p * qy + p * p * b.y];
}

function now() {   // time on the flow scale: live — now; replay — a point in the past (pos is a fraction of the window, 0…1)
  if (!replay) return Date.now() / 1000;
  return replay.from + (replay.to - replay.from) * replay.pos;
}

// the replay moves forward at its own speed; while paused or while the slider is dragged, it stands still
function advance(t) {
  const dt = t - replay.last;
  replay.last = t;
  if (replay.paused || seeking || replay.pos >= 1) return;
  replay.pos = Math.min(1, replay.pos + dt * speed / REPLAY_MS);
  if (replay.pos >= 1 && !replay.paused) togglePause();   // finished — stays at the end; ▶ restarts from the beginning
}

// seek: everything before the new point appears at once as link trails, without flying; from there on, as usual
function seek(p) {
  if (!replay) return;
  pulses = []; ripples = []; edges.clear(); signals = 0;
  for (const n of nodes.values()) { n.heat = 0; n.in = n.out = 0; }
  replay.pos = Math.max(0, Math.min(1, p)); replay.i = 0;
  notify();
  const t = now(), recent = [];
  while (replay.i < replay.items.length && replay.items[replay.i].ts <= t) {
    const f = replay.items[replay.i];
    if (f.kind === "gone") gone(f.from); else if (f.ts > t - holdSpan()) recent.push(f); else mark(f);
    replay.i += 1;
  }
  // recent ones before the point show who worked with whom: the last SEEK_FLY fly, the rest become trails
  recent.slice(0, -SEEK_FLY).forEach(mark);
  recent.slice(-SEEK_FLY).forEach((f, i) => fire(f, i * 120));
}

function setSpeed(v) {
  speed = v;
  speedSelect.value = String(v);
  speedSelect.title = hub.t("web.flow.speed_title", { sec: Math.round(REPLAY_MS / 1000 / v) });
  notify();
}

function togglePause() {
  if (!replay) return;
  if (replay.paused && replay.pos >= 1) seek(0);   // at the end ▶ replays from the start
  replay.paused = !replay.paused;
  pauseButton();
  notify();
}
function pauseButton() {   // the pause button follows the replay state
  const b = speedEl.querySelector(".pause"), paused = !!replay?.paused;
  b.classList.toggle("is-paused", paused);
  b.title = hub.t(paused ? "web.flow.resume" : "web.flow.pause");
  b.setAttribute("aria-label", b.title);
}

let laidFor = null, ticks = 0;
function frame() {
  raf = requestAnimationFrame(frame);
  // the page snapshot arrived or was updated (Flow was opened right away — there were no sessions yet): lay out by it, once per half second
  if (++ticks % 30 === 0 && laidFor !== (S.snap.sessions || null) && !seeking) layout();
  draw();
}

const isQuiet = (n) => !cur.big && quiet(n.id);
const dotSize = (n) => n.role === "hub" ? 9 : n.role === "owner" ? 8 : n.role === "agent" ? 6 : n.role === "exec" ? 3 : 4;

// a lead session with folded executors wears a thin dashed ring and its name ends with their number ("lead +3"); a
// click on it shows them as dots nearby. A working lead breathes with a ring of its own — the dashed one goes outside it
const foldedOf = (n) => (cur.big && !expanded.has(n.id) && cur.folded.get(n.id)) || [];
const foldRing = (n, busy) => dotSize(n) + (busy.has(n.id) ? 11 : 4);
function drawFolded(busy) {
  cx2d.setLineDash([2, 3]); cx2d.lineWidth = 1; cx2d.globalAlpha = 0.7;
  for (const n of nodes.values()) {
    if (!foldedOf(n).length) continue;
    cx2d.strokeStyle = colors[n.role] || colors.worker;
    cx2d.beginPath(); cx2d.arc(n.x, n.y, foldRing(n, busy), 0, Math.PI * 2); cx2d.stroke();
  }
  cx2d.setLineDash([]); cx2d.globalAlpha = 1;
}

// a label on the canvas sits on a pad of the page background: a line that passes under it does not cross the letters out
function padText(text, x, y, w, px, color) {
  cx2d.globalAlpha = 0.86; cx2d.fillStyle = colors.bg;
  cx2d.beginPath(); cx2d.roundRect(x - w / 2 - 4, y - px, w + 8, px + Math.round(px / 4) + 2, 4); cx2d.fill();
  cx2d.globalAlpha = 1; cx2d.fillStyle = color;
  cx2d.fillText(text, x, y);
}

// labels: on a small hub — for all; on a big one — for the key ones (you, hub, core, schedule), working ones, leads and
// the most active, as long as they do not overlap; the others appear on hover or touch
const widths = new Map();
const GAP = 4, NARROW = 700, EDGE = 12;   // px: a phone-wide canvas; a name starts 12px from the edge (its pad — 8px)
function drawLabels(busy) {
  lastLabels = [];
  const big = cur.big, boxes = [];
  const rank = (n) => n === hover || n === selected ? 1e12 : n.role === "hub" || n.role === "owner" ? 1e11 : n.role === "core" ? 1e10
    : busy.has(n.id) ? 1e9 + n.in + n.out : foldedOf(n).length ? 1e8 + n.in + n.out : n.role === "exec" ? 0 : n.in + n.out + 1;
  const list = big ? [...nodes.values()].sort((a, b) => rank(b) - rank(a)) : [...nodes.values()];
  const s = sess(), px = labelPx;   // label size and color come from --flow-label-size / --flow-label (the theme or the embedder)
  cx2d.textAlign = "center"; cx2d.textBaseline = "alphabetic";
  // one name: its place is taken in `boxes`; `limit` cuts it; the result is drawn by the caller
  const one = (n, limit, middle = false) => {
    const x0 = s.get(n.id), key = n.role === "hub" || n.role === "owner" || n.role === "core";
    let text = `${x0 && x0.icon ? x0.icon + " " : ""}${n.role === "hub" ? tx("web.flow.hub") : label(n.id)}`;
    // executors of one lead start alike and differ at the end: their names are cut in the middle
    if (big && text.length > limit) text = middle ? text.slice(0, Math.ceil(limit / 2)) + "…" + text.slice(-Math.floor(limit / 2))
      : text.slice(0, limit - 1) + "…";
    const pin = n === hover || n === selected, folded = foldedOf(n).length;
    if (isQuiet(n) && !pin) return null;   // a quiet dot is named when pointed at
    if (folded) text += ` +${folded}`;
    cx2d.font = `${n.role === "hub" || n.role === "owner" ? 600 : 500} ${px}px ${css("--font")}`;
    const width = (t) => {
      const wk = cx2d.font + t;
      if (!widths.has(wk)) widths.set(wk, cx2d.measureText(t).width);
      return widths.get(wk);
    };
    const d = dotSize(n), pinned = n === hover || n === selected;
    // every dot has its name: under the dot, and where that place is taken — above it, beside it or at a corner;
    // on a narrow canvas the dots are close, so a name is always below its dot — one side tells whose name it is
    const up = d + 6, down = d + px + 4, mid = Math.round(px / 3);
    let x, y, w;
    const place = (t) => {
      w = width(t);
      const side = d + 6 + w / 2;
      const places = W < NARROW ? [[0, down], [side, down], [-side, down], [0, down + px + 4]]
        : [[0, down], [0, -up], [side, mid], [-side, mid], [side, down], [-side, down], [side, -up], [-side, -up],
          [0, down + px + 4], [0, -up - px - 4]];
      for (const [dx, dy] of places) {
        x = Math.min(W - w / 2 - EDGE, Math.max(w / 2 + EDGE, n.x + dx)); y = Math.min(H - 4, Math.max(px + 2, n.y + dy));
        const b0 = { id: n.id, x0: x - w / 2, x1: x + w / 2, y0: y - px, y1: y + Math.round(px / 4) };
        // names do not touch: 4px between two of them; an executor's name does not lie on another dot either
        const onDot = middle && [...nodes.values()].some((m) => m !== n && m.x > b0.x0 - 3 && m.x < b0.x1 + 3
          && m.y > b0.y0 - 3 && m.y < b0.y1 + 3);
        if (!onDot && !boxes.some((b) => b0.x0 - GAP < b.x1 && b.x0 < b0.x1 + GAP && b0.y0 - GAP < b.y1 && b.y0 < b0.y1 + GAP)) return b0;
        if (!big || key || pinned) return b0;   // a hub that fits, and the key dots: under the dot, as before
      }
      return null;
    };
    const box = place(text);
    if (!box) return null;
    boxes.push(box);
    return Object.assign(box, { font: cx2d.font, text, x, y, w, pin: pinned });
  };
  const show = (b) => {
    if (!b) return;
    cx2d.font = b.font; lastLabels.push(b);
    padText(b.text, b.x, b.y, b.w, px, b.pin ? colors.fg : colors.label);
    b.color = cx2d.fillStyle;
  };
  // opened executors come last and share one length: no name is dropped while a shorter cut (down to 8 letters) gives
  // every one of them room between its neighbours, under its dot; the whole name is in the tip
  const late = big ? list.filter((n) => n.role === "exec" && n !== hover && n !== selected) : [];
  for (const n of list) if (!late.includes(n)) show(one(n, 22));
  for (let limit = 22, base = boxes.length; late.length; limit -= 2) {
    const got = late.map((n) => one(n, limit, true));
    if (limit <= 8 || got.every((b, i) => b || isQuiet(late[i]))) { got.forEach(show); break; }
    boxes.length = base;
  }
}

// what a line carries is written on the line itself (no legend below): one label per kind, on its freshest visible link,
// in the middle of the arc; a label that would cover a dot label is skipped
function drawKinds(flowNow) {
  lastKinds = [];
  const names = KIND_NAME(), fresh = new Map();
  for (const e of edges.values()) {
    if (!names[e.kind] || Math.max(0, flowNow - e.at) > (mode === "live" ? LIVE_WINDOW : holdSpan() * 0.9)) continue;
    if (!fresh.has(e.kind)) fresh.set(e.kind, []);
    fresh.get(e.kind).push(e);
  }
  const px = labelPx;
  const dots = [...nodes.values()].map((n) => { const d = dotSize(n) + 2; return { x0: n.x - d, x1: n.x + d, y0: n.y - d, y1: n.y + d }; });
  cx2d.font = `500 ${px}px ${css("--font")}`; cx2d.textAlign = "center"; cx2d.textBaseline = "alphabetic";
  for (const [kind, list] of fresh) {
    const text = names[kind], w = cx2d.measureText(text).width;
    // the freshest line of the kind whose middle is free: a label never covers a dot's name or another label
    for (const e of list.sort((p, q) => q.at - p.at)) {
      // the word lies along its own line, at the middle; a line shorter than the word carries none
      if (Math.hypot(e.b.x - e.a.x, e.b.y - e.a.y) < w + 24 + dotSize(e.a) + dotSize(e.b)) continue;
      const [mx, my] = bez(0.5, e.a, e.b, 0.12), [x1, y1] = bez(0.45, e.a, e.b, 0.12), [x2, y2] = bez(0.55, e.a, e.b, 0.12);
      let angle = Math.atan2(y2 - y1, x2 - x1);
      if (Math.cos(angle) < 0) angle += Math.PI;   // never upside down
      const hw = w / 2 + 4, hh = px / 2 + 3, bw = Math.abs(Math.cos(angle)) * hw + Math.abs(Math.sin(angle)) * hh,
        bh = Math.abs(Math.sin(angle)) * hw + Math.abs(Math.cos(angle)) * hh;
      if (mx - bw < 2 || mx + bw > W - 2 || my - bh < 2 || my + bh > H - 2) continue;
      const box = { kind, x0: mx - bw, x1: mx + bw, y0: my - bh, y1: my + bh, from: e.a.id, to: e.b.id, cx: mx, cy: my, angle, w };
      if ([...lastLabels, ...lastKinds].some((b) => box.x0 < b.x1 && b.x0 < box.x1 && box.y0 < b.y1 && b.y0 < box.y1)) continue;
      if (dots.some((b) => box.x0 < b.x1 && b.x0 < box.x1 && box.y0 < b.y1 && b.y0 < box.y1)) continue;   // nor a dot
      lastKinds.push(box);
      cx2d.save(); cx2d.translate(mx, my); cx2d.rotate(angle);
      padText(text, 0, px / 3, w, px, colors[kind] || colors.label);
      cx2d.restore();
      break;
    }
  }
}

// "Now": while a signal is in the window, its line stays visible
function lineAlpha(e, flowNow) {
  const age = Math.max(0, flowNow - e.at);
  return Math.max(mode === "live" && age < LIVE_WINDOW ? LIVE_LINE : 0, Math.max(0, 1 - age / holdSpan()) * Math.min(0.5, 0.08 + e.n * 0.04));
}

function draw() {
  const t = clock();
  if (replay) advance(t);
  const flowNow = now();
  if (replay) step(flowNow);
  cx2d.clearRect(0, 0, W, H);
  // links: thin arcs, brighter the fresher and more frequent
  for (const e of edges.values()) {
    const alpha = lineAlpha(e, flowNow);
    if (alpha < 0.01) continue;
    cx2d.strokeStyle = colors.muted; cx2d.globalAlpha = alpha; cx2d.lineWidth = 1;
    cx2d.setLineDash(e.mod ? [4, 4] : []);
    cx2d.beginPath(); cx2d.moveTo(e.a.x, e.a.y);
    const [qx, qy] = bez(0.5, e.a, e.b, 0.12);
    cx2d.quadraticCurveTo(2 * qx - (e.a.x + e.b.x) / 2, 2 * qy - (e.a.y + e.b.y) / 2, e.b.x, e.b.y); cx2d.stroke();
  }
  cx2d.setLineDash([]);
  cx2d.globalAlpha = 1;
  // dots
  const busy = new Set([...sess().values()].filter((x) => x.state === "busy").map((x) => x.name));
  for (const n of nodes.values()) {
    n.x += (n.tx - n.x) * 0.08; n.y += (n.ty - n.y) * 0.08; n.heat *= 0.985;
    const base = dotSize(n);
    const c = colors[n.role] || colors.worker;
    if (isQuiet(n) && n !== hover && n !== selected) {   // quiet: the same size, an empty ring — no fill, no glow
      cx2d.globalAlpha = 1; cx2d.strokeStyle = c; cx2d.lineWidth = 1.5;
      cx2d.beginPath(); cx2d.arc(n.x, n.y, base - 0.75, 0, Math.PI * 2); cx2d.stroke();
      continue;
    }
    const glow = base * (2.4 + n.heat * 1.6);
    const g = cx2d.createRadialGradient(n.x, n.y, 0, n.x, n.y, glow);
    g.addColorStop(0, c); g.addColorStop(1, "transparent");
    cx2d.globalAlpha = 0.18 + Math.min(0.5, n.heat * 0.3); cx2d.fillStyle = g;
    cx2d.beginPath(); cx2d.arc(n.x, n.y, glow, 0, Math.PI * 2); cx2d.fill();
    if (busy.has(n.id)) {   // working — breathes with a ring
      cx2d.globalAlpha = 0.35 + 0.25 * Math.sin(t / 300); cx2d.strokeStyle = c; cx2d.lineWidth = 1.5;
      cx2d.beginPath(); cx2d.arc(n.x, n.y, base + 5 + 2 * Math.sin(t / 300), 0, Math.PI * 2); cx2d.stroke();
    }
    cx2d.globalAlpha = 1; cx2d.fillStyle = c;
    cx2d.beginPath(); cx2d.arc(n.x, n.y, base, 0, Math.PI * 2); cx2d.fill();
  }
  drawFolded(busy);
  drawLabels(busy);
  if (tipFor) placeTip();
  drawKinds(flowNow);
  // comets
  pulses = pulses.filter((p) => {
    const k = (t - p.start) / FLY_MS;
    if (k < 0) return true;
    if (k >= 1) { p.b.heat = Math.min(3, p.b.heat + 1); p.b.in += 1; ripples.push({ n: p.b, kind: p.kind, start: t }); return false; }
    const c = colors[p.kind] || colors.hub;
    for (let i = 10; i >= 0; i--) {
      const q = Math.max(0, k - i * 0.022);
      const [x, y] = bez(easeInOut(q), p.a, p.b, p.bend);
      cx2d.globalAlpha = (1 - i / 11) * 0.9; cx2d.fillStyle = c;
      cx2d.beginPath(); cx2d.arc(x, y, i ? 2.4 * (1 - i / 12) : 3.4, 0, Math.PI * 2); cx2d.fill();
    }
    const [hx, hy] = bez(easeInOut(k), p.a, p.b, p.bend);
    const g = cx2d.createRadialGradient(hx, hy, 0, hx, hy, 14);
    g.addColorStop(0, c); g.addColorStop(1, "transparent");
    cx2d.globalAlpha = 0.45; cx2d.fillStyle = g; cx2d.beginPath(); cx2d.arc(hx, hy, 14, 0, Math.PI * 2); cx2d.fill();
    return true;
  });
  // ripples on the water at the receiver
  ripples = ripples.filter((r) => {
    const k = (t - r.start) / 900;
    if (k < 0) return true;   // a delayed seek ripple: waits for its start (previously a negative radius cut the frame short)
    if (k >= 1) return false;
    cx2d.globalAlpha = (1 - k) * 0.6; cx2d.strokeStyle = colors[r.kind] || colors.hub; cx2d.lineWidth = 2 * (1 - k) + 0.5;
    cx2d.beginPath(); cx2d.arc(r.n.x, r.n.y, 8 + k * 26, 0, Math.PI * 2); cx2d.stroke();
    return true;
  });
  cx2d.globalAlpha = 1;
  if (replay) {
    bar.style.width = `${replay.pos * 100}%`;
    if (!seeking) seekEl.value = String(Math.round(replay.pos * 1000));
    seekEl.style.setProperty("--p", `${replay.pos * 100}%`);   // the elapsed part of the line is colored
    clockEl.textContent = hub.clock?.(flowNow, { day: "numeric", month: "short" })   // a page not reloaded after an update has none
      ?? new Date(flowNow * 1000).toLocaleString(hub.locale, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }
  notify();   // the replay position goes to hubflow:state listeners (notify itself skips frames with no change)
}
const easeInOut = (x) => x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2;

// replay: release signals whose time on the scale has come (a batch is spread out so they do not stick together)
function step(flowNow) {
  let i = 0;
  while (replay.i < replay.items.length && replay.items[replay.i].ts <= flowNow) {
    const f = replay.items[replay.i];
    if (f.kind === "gone") gone(f.from); else fire(f, (i++ % 6) * 90);
    replay.i += 1;
  }
}

async function pollLive() {
  if (mode !== "live" || !(S.snap.sessions || []).length) return;   // no snapshot yet — it is unknown whom to draw: do not lose signals
  try {
    const g = gen, r = await fetch(`/api/flow?since=${last}`);
    const { flow } = await r.json();
    if (mode !== "live" || g !== gen) return;   // the mode was switched while we waited: the response belongs to the previous window
    const fresh = last === 0, names = alive();
    for (const f of flow) if (f.kind !== "gone" && liveOk(f, names)) note(f);   // the window is computed over the whole batch at once, not per signal
    if (cur.big || fresh) layout();   // the window moved: new ones go onto the canvas, those that went quiet leave
    for (const [i, f] of flow.entries()) {
      last = Math.max(last, f.seq);
      if (f.kind === "gone") { gone(f.from); continue; }
      if (!liveOk(f, names)) continue;   // those no longer present are not drawn
      if (fresh) {   // first open: recent signals as links without flying, the last few with a flight
        if (i >= flow.length - 6) fire(f, (i - flow.length + 6) * 180);
        else {
          const a = resolve(f.from), b = resolve(f.to);
          if (a && b && a !== b) edgeAdd(a, b, f);
        }
      } else fire(f, i * 140);
    }
    if (cur.big && flow.some((f) => f.kind === "gone")) layout();   // those gone in this batch are removed from the plan and the count
  } catch { /* the connection will return — next poll */ }
}

async function setMode(m) {
  const g = ++gen;
  mode = m; replay = null; loading = m !== "live"; failed = false; seeking = false;   // the previous window is no longer a replay: its bounds are not valid
  periodSelect.value = m;
  pulses = []; ripples = []; edges.clear(); signals = 0;
  for (const n of nodes.values()) { n.heat = 0; n.in = n.out = 0; }
  touched = new Map(); expanded = new Set(); selected = null;   // each window has its own activity
  root.classList.toggle("replaying", m !== "live");
  clockEl.hidden = m === "live";
  bar.style.width = "0";
  seekEl.value = "0"; seekEl.style.setProperty("--p", "0%");   // the previous window's slider does not stay where it was
  pauseButton();
  if (m === "live") { last = 0; clockEl.textContent = hub.t("web.flow.clock_now"); layout(); notify(); pollLive(); return; }
  notify();
  const hours = { h1: 1, h6: 6, h24: 24 }[m];
  clockEl.textContent = hub.t("web.flow.loading");
  let flow;
  try {
    const r = await fetch(`/api/flow/history?hours=${hours}`);
    if (!r.ok) throw new Error(String(r.status));
    ({ flow } = await r.json());
    if (!Array.isArray(flow)) throw new Error("flow");
  } catch {   // the journal did not arrive: do not hang in "loading" — say so and wait for a new period choice
    if (g !== gen) return;
    loading = false; failed = true; clockEl.textContent = hub.t("web.flow.load_failed");
    periodSelect.selectedIndex = -1;   // selection cleared: the same period fires change again (a retry) instead of silence
    notify();
    return;
  }
  if (g !== gen) return;   // another period (or the same one again) was chosen while we waited: the response belongs to the previous choice
  const to = Date.now() / 1000;
  loading = false;
  replay = { items: flow, i: 0, from: to - hours * 3600, to, pos: 0, last: clock(), paused: false };
  for (const f of flow) if (f.kind !== "gone") note(f);
  if (!plan().big) for (const f of flow) if (f.kind !== "gone") { ensure(f.from); ensure(f.to); }
  layout();
  notify();
}

// the dot under the cursor or finger (the nearest within the radius)
function hit(x, y, rad) {
  let best = null, d0 = rad;
  for (const n of nodes.values()) { const d = Math.hypot(n.x - x, n.y - y); if (d < d0) { d0 = d; best = n; } }
  return best;
}
const toCanvas = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };

function showTip(n) {
  const tip = root.querySelector(".flow-tip");
  if (!n) { tip.hidden = true; tipFor = tipBox = null; return; }
  const s = sess().get(n.id);
  tip.hidden = false;
  tip.replaceChildren(el("b", null, n.role === "hub" ? tx("web.flow.hub") : label(n.id)),
    el("span", null, s ? (s.about || s.label || "") : ""),
    ...(n.out || n.in ? [el("span", "flow-tip-counts", tx("web.flow.tip_counts", { sent: n.out, received: n.in }))] : []));
  // measured once per tip: a frame that puts it in place again must not force a layout
  tipFor = n;
  tipBox = { tip, w: tip.offsetWidth || 200, h: tip.offsetHeight || 40, left: canvas.offsetLeft, top: canvas.offsetTop };
  placeTip();
}
// beside the dot, on the side where it covers the fewest names (its own dot's name too) and no other dot; the dots move after a press
// (executors come out), so a frame puts the shown tip in place again
let tipFor = null, tipBox = null;
function placeTip() {
  if (!tipFor || !tipBox) return;
  const n = tipFor, { tip, w, h } = tipBox, d = dotSize(n) + 10;
  // a covered name, word on a line or dot counts for three, a name closer than 4px to the tip (its plate is 4px wider
  // than the text) — one
  const names = [...lastLabels, ...lastKinds];
  const over = (x, y) => [0, 0, 8].reduce((sum, m) => sum
    + names.filter((b) => x - m < b.x1 && b.x0 < x + w + m && y - m < b.y1 && b.y0 < y + h + m).length, 0)
    + 3 * [...nodes.values()].filter((o) => { const r = dotSize(o) + 4;   // 4px between the tip and a dot
      return o !== n && o.x > x - r && o.x < x + w + r && o.y > y - r && o.y < y + h + r; }).length;
  // the nearest such place: next to the dot, then a step of 40px further away
  const spots = [0, 40, 80, 120, 160, 200].flatMap((k) => [[n.x + d + k, n.y - 10], [n.x - d - k - w, n.y - 10], [n.x - w / 2, n.y - d - k - h],
    [n.x - w / 2, n.y + d + k + labelPx + 8]])
    .filter(([x, y], i) => !i || (x >= 0 && y >= 0 && x + w <= W && y + h <= H))   // on the canvas (the first one is clamped)
    .map(([x, y]) => [Math.max(0, Math.min(x, W - w)), Math.max(0, Math.min(y, H - h))])
    // and a step aside from each, on the canvas: a place that covers a dot by its corner is free 16px away
    .flatMap(([x, y]) => [0, -16, 16, -32, 32, -48, 48].map((dx) => [Math.max(0, Math.min(x + dx, W - w)), y]))
    .map(([x, y]) => ({ x, y, over: over(x, y) }));
  let best = spots.reduce((a, b) => (b.over < a.over ? b : a));
  // a busy canvas (a phone, many executors) may have no free place among those: the nearest free one anywhere on it,
  // and on a canvas with none — the nearest that covers the least
  if (best.over) {
    const all = [];
    for (let y = 8; y + h <= H - 8; y += 12) for (let x = 8; x + w <= W - 8; x += 12) all.push([x, y]);   // 8px off the edge
    all.sort(([ax, ay], [bx, by]) => Math.hypot(ax + w / 2 - n.x, ay + h / 2 - n.y) - Math.hypot(bx + w / 2 - n.x, by + h / 2 - n.y));
    for (const [x, y] of all) {
      const o = over(x, y);
      if (o < best.over) best = { x, y, over: o };
      if (!o) break;
    }
  }
  // the tip is a child of the view, the canvas lies under the bar: canvas coordinates, moved by the canvas's own place
  tip.style.left = `${tipBox.left + best.x}px`; tip.style.top = `${tipBox.top + best.y}px`;
}

function onMove(e) {
  const [x, y] = toCanvas(e);
  hover = hit(x, y, 16);
  canvas.style.cursor = hover && (foldedOf(hover).length || !["owner", "core"].includes(hover.role)) ? "pointer" : "default";
  showTip(hover || selected);
}

// click: on a lead with folded executors — show them as dots nearby; on any other dot (and on that lead again) — go to
// the session; past the dots — fold the executors back. With a finger, the first touch only labels the dot (a phone has
// no hover), the second one on it navigates
let touchTap = false;
function onClick(e) {
  const [x, y] = toCanvas(e), touch = touchTap;
  const n = hit(x, y, touch ? 24 : 16);
  if (!n) { selected = null; showTip(null); if (expanded.size) { expanded.clear(); layout(); } return; }
  if (foldedOf(n).length) { expanded.add(n.id); layout(); hover = null; showTip(selected = n); return; }
  if (touch && selected !== n) { selected = n; hover = null; showTip(n); return; }
  if (!["owner", "core"].includes(n.role)) location.hash = `#/s/${encodeURIComponent(n.id)}`;
}

// period and speed are a plain select (as in "Schedule"): the keyboard, touch and the dropdown list are the browser's
function choice(cls, label, options, change) {
  const select = el("select", "btn flow-control " + cls);
  select.setAttribute("aria-label", label);
  for (const [value, text] of options) { const option = el("option", null, text); option.value = String(value); select.append(option); }
  select.addEventListener("change", () => change(select.value));
  return select;
}

function flowView() {
  if (!root) {
    root = el("div", "flow");
    canvas = el("canvas");
    cx2d = canvas.getContext("2d");
    const bar0 = el("div", "flow-bar");
    const controls = el("div", "flow-controls");
    periodSelect = choice("flow-period", hub.t("web.flow.period"),
      [["live", hub.t("web.flow.mode_now")], ["h1", hub.t("web.flow.mode_h1")], ["h6", hub.t("web.flow.mode_h6")], ["h24", hub.t("web.flow.mode_day")]], setMode);
    speedEl = el("div", "flow-speed");
    const pause = el("button", "btn flow-control pause"); pause.type = "button"; pause.title = hub.t("web.flow.pause");
    pause.setAttribute("aria-label", pause.title);
    const glyph = el("span", "flow-pause-icon"); glyph.setAttribute("aria-hidden", "true"); pause.append(glyph);
    pause.addEventListener("click", togglePause);
    speedSelect = choice("flow-rate", hub.t("web.flow.speed"), SPEEDS.map(([text, value]) => [value, text]), value => setSpeed(Number(value)));
    for (const o of speedSelect.options) o.title = hub.t("web.flow.speed_title", { sec: Math.round(REPLAY_MS / 1000 / Number(o.value)) });
    speedEl.append(pause, speedSelect);
    controls.append(periodSelect, speedEl);
    setSpeed(speed);
    // the clock is the replay's: live, the period list already says "Now"
    clockEl = el("span", "flow-clock", hub.t("web.flow.clock_now")); clockEl.hidden = mode === "live";
    noteEl = el("span", "flow-note"); noteEl.hidden = true;
    bar0.append(el("h2", null, hub.t("web.flow.title")), controls, clockEl, noteEl);
    bar = el("div", "flow-progress");
    seekEl = el("input", "flow-seek");
    seekEl.type = "range"; seekEl.min = "0"; seekEl.max = "1000"; seekEl.value = "0";
    seekEl.setAttribute("aria-label", hub.t("web.flow.seek"));
    // while dragging, the replay stands still and the frame does not move the slider from under the finger; on release (or a cancelled touch) it goes on
    seekEl.addEventListener("pointerdown", () => { seeking = true; });
    seekEl.addEventListener("input", () => { seeking = true; seek(Number(seekEl.value) / 1000); });
    const release = () => { seeking = false; };
    for (const ev of ["pointerup", "pointercancel", "change", "blur"]) seekEl.addEventListener(ev, release);
    const tip = el("div", "flow-tip"); tip.hidden = true; tipFor = tipBox = null;
    root.append(bar0, bar, seekEl, canvas, tip);
    document.addEventListener("keydown", (e) => {   // Space pauses the replay while Flow is on screen
      // Space does not open Flow's own lists: after picking a period with the mouse, focus stays in the list, and Space is pause
      const own = e.target.closest?.(".flow-controls select");
      if (e.key !== " " || !replay || !raf || (!own && e.target.closest?.("input, textarea, select, [contenteditable]"))) return;
      e.preventDefault(); togglePause();
    });
    canvas.addEventListener("pointerdown", (e) => { touchTap = e.pointerType === "touch"; });
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", () => { hover = null; showTip(selected); });
    canvas.addEventListener("click", onClick);
    new ResizeObserver(resize).observe(canvas);
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", readColors);
    window.addEventListener("hub-theme", readColors);   // the theme was changed in Settings (js/themes.js)
  }
  return root;
}

// demo recording: stop our own frames and draw on command — step(ms) advances time by exactly one step
window.__flowRec = {
  begin() { cancelAnimationFrame(raf); raf = -1; fakeNow = performance.now(); },
  mode: (m) => setMode(m),
  step(ms) { fakeNow += ms; draw(); },
  done: () => !replay,
  pos: () => (replay ? replay.pos : null),
  pulses: () => pulses.length,
  running: () => raf > 0,
  nodes: () => [...nodes.keys()],
  // how many dots are on the canvas and why not all: a big hub, executors folded under their leads, those cut by the limit
  summary: () => ({ big: cur.big, shown: cur.shown, total: cur.total, hidden: cur.cut.size,
    folded: Object.fromEntries([...cur.folded].filter(([l, v]) => v.length && !expanded.has(l)).map(([l, v]) => [l, v.length])) }),
  labels: () => lastLabels.map((b) => ({ ...b })),
  // where to click to show a lead's folded executors (the lead's dot; null — nothing is folded under it)
  badge: (id) => { const n = nodes.get(id); return n && foldedOf(n).length ? { x: n.x, y: n.y } : null; },
  signals: () => signals,
  kinds: () => lastKinds.map((b) => ({ ...b })),
  // the lines a frame draws now: from, to, alpha
  lines: () => { const t = now(); return [...edges.values()].map((e) => ({ from: e.a.id, to: e.b.id, alpha: lineAlpha(e, t) }))
    .filter((l) => l.alpha >= 0.01); },
  quiet: () => [...nodes.values()].filter(isQuiet).map((n) => n.id),
  ring: (id) => { const n = nodes.get(id), busy = new Set([...sess().values()].filter((x) => x.state === "busy").map((x) => x.name));
    return n ? { fold: foldedOf(n).length ? foldRing(n, busy) : null, pulse: busy.has(id) && !isQuiet(n) ? dotSize(n) + 7 : null } : null; },
  at: (id) => { const n = nodes.get(id); return n ? { x: n.x, y: n.y, tx: n.tx, ty: n.ty } : null; },
  tip: () => { const t = root?.querySelector(".flow-tip"); return t && !t.hidden ? [...t.children].map((c) => c.textContent) : null; },
  tipBox: () => { const t = root?.querySelector(".flow-tip"); return t && !t.hidden ? { x0: t.offsetLeft - canvas.offsetLeft,
    y0: t.offsetTop - canvas.offsetTop, x1: t.offsetLeft - canvas.offsetLeft + t.offsetWidth, y1: t.offsetTop - canvas.offsetTop + t.offsetHeight } : null; },
};

// control from outside — for whoever embeds Flow (a plugin, an app): commands are window.hubFlow.*, the state is the
// hubflow:state event on window (detail as in hubFlow.state()) on every change: mode and window loading, speed, pause,
// the replay position in 0.5% steps, whether the canvas is on screen
const flowState = () => ({ mode, loading, failed, speed, paused: !!replay?.paused, pos: replay ? replay.pos : 0, replay: !!replay,
  from: replay?.from ?? null, to: replay?.to ?? null, speeds: SPEEDS.map((x) => x[1]), active: raf !== 0 });
let lastSent = "";
function notify() {   // also called on every frame: the key is made of simple values, the state is built only on change
  const key = [mode, loading, failed, speed, !!replay?.paused, replay ? Math.round(replay.pos * 200) : -1, replay?.from, replay?.to, raf !== 0].join("|");
  if (key === lastSent) return;
  lastSent = key;
  window.dispatchEvent(new CustomEvent("hubflow:state", { detail: flowState() }));
}
window.hubFlow = {
  state: flowState,
  // commands work while Flow is on screen (state().active): without a canvas there is nowhere to load and lay out a window
  setMode: (m) => { if (raf && periodSelect && ["live", "h1", "h6", "h24"].includes(m)) setMode(m); },
  setSpeed: (v) => { const x = Number(v); if (raf && speedSelect && SPEEDS.some((s) => s[1] === x)) setSpeed(x); },
  togglePause: () => { if (raf) togglePause(); },
  // seek(p, true) — their own slider is still being dragged: the replay stands still until seek(p) comes without it
  seek: (p, dragging = false) => { const x = Number(p); if (raf && replay && Number.isFinite(x)) { seeking = !!dragging; seek(x); } },
  setLang: (code) => { setLang(code); },
};

function flowStart() {
  if (raf) { layout(); return; }
  readColors();
  if (!resize()) layout();   // the same size as before leaving Flow — the layout is still made from a fresh snapshot
  raf = requestAnimationFrame(frame);
  poll = setInterval(pollLive, 1500);
  setMode("live");
}

function flowStop() {
  if (!raf) return;
  cancelAnimationFrame(raf); raf = 0;
  clearInterval(poll); poll = 0; replay = null; loading = false; failed = false; seeking = false; mode = "live"; gen += 1;   // left Flow — the window's response is no longer needed
  notify();
}

export default function register(h) {
  hub = h;
  hub.addStyle("flow.css");
  hub.addView({
    id: "flow", title: hub.t("web.flow.title"), subtitle: hub.t("web.flow.subtitle"), icon: "flow", full: true,
    render(box) { if (!box.firstChild) box.append(flowView()); },
    show: flowStart,
    hide: flowStop,
  });
}
