// Settings → Accounts (switching between tokens, and viewing the limits of all connected accounts; part of the core,
// not a plugin). The pool's tokens in order, which one is current, the 5-hour and weekly limits of each (the last
// measurement — taken while the hub was running on it, or by "Refresh"), who is busy until reset; "Switch" moves to
// that account now, "Refresh" takes a fresh limits reading without a switch (/api/accounts/probe).
// The core does the switching (modules/accounts); the pool is edited with `hub accounts …`, the page never sees
// secrets. The limits line under the input field leads here.
import { el } from "./dom.js";
import { clock } from "./clock.js";
import { post } from "./api.js";
import { S, render } from "./state.js";
import { locale, t } from "./i18n.js";

const WIN = [["five_hour", t("web.accounts.win_five_hour")], ["seven_day", t("web.accounts.win_week")]];

// time without a preposition: today — just the hours, otherwise date and hours
function stamp(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const hm = clock(ts, locale);
  if (d.toDateString() === now.toDateString()) return hm;
  return d.toLocaleDateString(locale, { weekday: "short", day: "numeric", month: "short" }) + `, ${hm}`;
}
function when(ts) {
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString() ? t("web.accounts.at_time", { time: stamp(ts) }) : stamp(ts);
}

// catalog text with {name} placeholders — <code> nodes in place of the substitutions
function withCode(text, codes) {
  return text.split(/\{(\w+)\}/).map((part, i) => (i % 2 ? el("code", null, codes[part] ?? `{${part}}`) : part));
}

function bar(label, w, now) {
  const row = el("div", "ac-lim");
  // the reset has passed (the same boundary as core/executors.reset_passed): the measurement is of the previous window —
  // not "0%" with an empty bar, but the words of `hub accounts` and "Spending"
  const gone = !!(w && w.resets && w.resets <= now);
  if (!w || typeof w.used !== "number" || gone) {
    row.append(el("span", "ac-lim-name", label),
      el("span", "ac-muted ac-lim-note", gone ? t("web.accounts.window_reset") : t("web.accounts.no_reading")));
    return row;
  }
  const used = Math.round(w.used * 100);
  row.dataset.level = used >= 90 ? "high" : used >= 75 ? "warn" : "ok";
  const track = el("span", "ac-bar");
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  track.setAttribute("aria-valuenow", String(Math.min(used, 100)));
  track.setAttribute("aria-label", `${label}: ${used}%`);
  const fill = el("span");
  fill.style.width = `${Math.min(used, 100)}%`;
  track.append(fill);
  row.append(el("span", "ac-lim-name", label), track, el("span", "ac-pct", `${used}%`),
    el("span", "ac-muted", w.resets ? t("web.accounts.resets", { when: when(w.resets) }) : ""));
  return row;
}

// "Refresh" (/api/accounts/probe): a fresh limits reading without a switch. The tab is rebuilt with every core
// snapshot, so what is being measured and the latest failures live here, by account name ("*" — "Refresh all"; its
// request failure is shown under that button, an account's own under its card); a failure is shown for PROBE_KEEP, the
// card's new reading comes with the snapshot. The server takes one reading at a time: while one runs, every button waits
const probing = new Set(), probeErr = new Map();
const PROBE_KEEP = 120000;
const redraw = () => { if (S.selected === ":settings") render(); };

async function runProbe(name) {
  const key = name || "*";
  probing.add(key);
  for (const k of name ? [name, "*"] : [...probeErr.keys()]) probeErr.delete(k);
  redraw();
  const r = await post("/api/accounts/probe", name ? { account: name } : {});
  probing.delete(key);
  const at = Date.now();
  if (r.error) probeErr.set(key, { text: r.error, at });
  for (const x of r.data?.results || []) if (!x.ok) probeErr.set(x.name, { text: x.error || "", at });
  redraw();
}

function probeError(key) {
  const e = probeErr.get(key);
  if (e && Date.now() - e.at > PROBE_KEEP) probeErr.delete(key);
  return probeErr.get(key)?.text || "";
}

function probeButton(name) {
  const busy = probing.has(name || "*") || (!!name && probing.has("*"));
  const b = el("button", "btn quiet small ac-probe", busy ? t("web.accounts.probing") : t(name ? "web.accounts.probe" : "web.accounts.probe_all"));
  b.type = "button";
  b.title = t(name ? "web.accounts.probe_title" : "web.accounts.probe_all_title");
  b.dataset.probe = name || "*";
  b.disabled = probing.size > 0;
  b.addEventListener("click", () => runProbe(name));
  return b;
}

// account name with its email, if one is recorded in the pool (`hub accounts email`): "work (me@example.com)"
function accountLabel(name, pool) {
  const email = (pool || []).find((a) => a.name === name)?.email;
  return email ? `${name} (${email})` : name;
}

function card(a, acc, now, i, on) {
  const active = a.name === acc.active;
  const until = (acc.blocked || {})[a.name] || 0;
  const seen = (acc.seen || {})[a.name] || {};
  const box = el("section", "ac-card");
  if (active) box.classList.add("on");
  const head = el("div", "ac-head");
  const title = el("div", "ac-title");
  title.append(el("span", "ac-num", String(i + 1)), el("span", "ac-name", a.name));
  if (a.email) { const kind = el("span", "ac-kind", a.email); kind.title = a.email; title.append(kind); }
  head.append(title);
  if (active) head.append(el("span", "ac-badge", acc.pinned ? t("web.accounts.badge_now_manual") : t("web.accounts.badge_now")));
  else if (until > now) head.append(el("span", "ac-badge busy", t("web.accounts.busy_until", { time: stamp(until) })));
  else if (on) {
    const go = el("button", "btn quiet ac-go", t("web.accounts.go"));
    go.type = "button";
    go.title = t("web.accounts.go_title");
    go.addEventListener("click", async () => {
      if (go.dataset.sure !== "1") { go.dataset.sure = "1"; go.textContent = t("web.accounts.go_sure"); return; }
      go.disabled = true;
      const r = await post("/api/accounts/use", { name: a.name });
      go.textContent = r.error ? r.error : t("web.accounts.going");
    });
    head.append(go);
  }
  box.append(head);
  for (const [key, label] of WIN) box.append(bar(label, (seen.windows || {})[key], now));
  const note = el("div", "ac-note-row");
  note.append(el("p", "ac-note", seen.ts ? (seen.status === "rejected" ? t("web.accounts.measured_refused", { when: when(seen.ts) }) : t("web.accounts.measured", { when: when(seen.ts) }))
    : t("web.accounts.no_limits_yet")));
  if (a.probe) note.append(probeButton(a.name));
  box.append(note);
  const err = a.probe && probeError(a.name);
  if (err) box.append(el("p", "ac-note ac-probe-err", err));
  return box;
}

// accounts gateway (modules/gateway, off by default): a switch with an explanation and the state. Turning it on does
// not restart sessions (running ones go direct until their own restart); turning it off restarts the sessions on the
// gateway with the same conversation, hence it takes a second click. The tab is drawn anew with every snapshot (the new
// view is built while the old one is still on the screen): the box on the screen is kept while the core has the same
// on/off and level, with the snapshot's words in the line under it — a new box lost what the click made it say (the
// second click asked for, "…" while the request runs, the answer)
let gwBox = null, gwKey = "";

function gatewayBox(g) {
  const key = `${!!g.enabled}|${g.level || "off"}`;
  if (gwBox?.isConnected && gwKey === key) {
    gwBox.querySelector(".ac-gw-state").textContent = g.text || "";
    return gwBox;
  }
  const box = el("section", "ac-gw");
  gwBox = box;
  gwKey = key;
  const l = el("label", "opt");
  const sw = el("input");
  sw.type = "checkbox";
  sw.checked = !!g.enabled;
  const txt = el("span");
  const note = el("small", "ac-gw-note");
  txt.append(document.createTextNode(t("web.accounts.gateway")),
    el("small", null, t("web.accounts.gateway_hint")),
    note);
  l.append(sw, txt);
  sw.addEventListener("change", async () => {
    if (!sw.checked && g.level !== "off" && sw.dataset.sure !== "1") {
      sw.dataset.sure = "1";
      sw.checked = true;
      note.textContent = t("web.accounts.gateway_off_sure");
      note.dataset.kind = "sure";   // a hook for themes: what the note says
      return;
    }
    sw.disabled = true;
    note.textContent = "…";
    delete note.dataset.kind;
    const want = sw.checked;
    const r = await post("/api/settings/set", { key: "gateway.enabled", value: want });
    note.textContent = r.error ? t("web.updates.failed", { error: r.error }) : want ? t("web.accounts.gateway_starting") : t("web.accounts.gateway_off");
    if (!r.error && want) note.dataset.kind = "starting";
    sw.disabled = false;   // the box may stay now: the switch answers again (a refused one goes back to the core's position)
    if (r.error) sw.checked = !want;
  });
  const state = el("p", "ac-gw-state", g.text || "");
  state.dataset.level = g.level || "off";
  box.append(l, state);
  return box;
}

// the Accounts tab in Settings: redrawn with the core snapshot
export function accountsSection() {
  const conf = S.snap?.accounts || { pool: [] }, acc = S.snap?.account || {}, now = Date.now() / 1000;
  const wrap = el("div", "ac");
  const pool = conf.pool || [];
  const top = el("p", "ac-sum");
  const current = acc.active ? accountLabel(acc.active, pool) : t("web.accounts.default_login");
  top.textContent = !pool.length
    ? t("web.accounts.pool_empty")
    : !conf.on
      ? t("web.accounts.pool_paused")
      : acc.since ? t("web.accounts.pool_now_since", { name: current, time: stamp(acc.since),
        pct: Math.round((conf.switch_at || 0.95) * 100) })
        : t("web.accounts.pool_now", { name: current, pct: Math.round((conf.switch_at || 0.95) * 100) });
  wrap.append(top);
  if (conf.gateway) wrap.append(gatewayBox(conf.gateway));
  if (conf.on && acc.exhausted && acc.exhausted.until > now) {
    wrap.append(el("p", "ac-stop", t("web.accounts.pool_exhausted", { when: when(acc.exhausted.until) })));
  }
  if (pool.filter((a) => a.probe).length > 1) {   // right above the cards it refreshes
    const row = el("div", "ac-tools");
    row.append(probeButton(null));
    wrap.append(row);
    const err = probeError("*");
    if (err) wrap.append(el("p", "ac-note ac-probe-err ac-tools-err", err));
  }
  const list = el("div", "ac-list");
  pool.forEach((a, i) => list.append(card(a, conf.on ? acc : { ...acc, active: null }, now, i, conf.on)));
  wrap.append(list);
  const how = el("p", "ac-note");
  how.append(...withCode(t("web.accounts.how"), { setup: "claude setup-token", add: t("web.accounts.how_add"),
    move: t("web.accounts.how_move"), off: "hub accounts off" }));
  wrap.append(how);
  if (pool.length) {   // replacing a token happens in place, not remove + add (that drops out of the pool for a moment and lands at the end)
    const tok = el("p", "ac-note");
    tok.append(...withCode(t("web.accounts.how_token"), { token: t("web.accounts.how_token_cmd") }));
    wrap.append(tok);
  }
  return wrap;
}
