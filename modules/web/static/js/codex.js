// "Settings" -> "Codex" on a hub with the Codex host — instead of Claude's "Accounts". The
// hub's Codex sign-in by a device code (into the hub's Codex folder, not the personal ~/.codex): "Sign in" gives a link
// and a code to enter on a phone or in a browser; email and plan, whether the sign-in was lost, the account limits (the
// core: account.limits). The Codex account pool (core/codexslot.py): a row per account — its sign-in, limits, busy or
// lost, the active one; "Add a Codex account" and a row's "Sign in" give the same link and code.
import { el } from "./dom.js";
import { clock } from "./clock.js";
import { post } from "./api.js";
import { S } from "./state.js";
import { locale, t } from "./i18n.js";
import { pctText } from "./limitpct.js";

const WIN = { five_hour: () => t("web.codex.win_five_hour"), seven_day: () => t("web.codex.win_week") };
// every reading here is Codex's: the share left, as its /status says ("47% left")
const left = (pct) => pctText(pct, true, t("web.limits.left"));

function when(ts) {
  return clock(ts, locale, { day: "numeric", month: "short" });
}

function codeBox(url, code, account) {
  const box = el("div", "cx-code");
  const a = el("a", null, url);
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  if (account) box.append(el("p", "cx-for", t("web.codex.code_for", { name: account })));
  box.append(el("p", null, t("web.codex.open")), a, el("p", null, t("web.codex.enter_code")), el("code", "cx-usercode", code),
    el("p", "ac-muted", t("web.codex.waiting")));
  return box;
}

function windowsLine(windows, now) {
  const parts = [];
  for (const [name, w] of Object.entries(windows || {})) {
    if (typeof w?.used !== "number" || !WIN[name]) continue;
    const gone = w.resets && w.resets < now;
    parts.push(`${WIN[name]()} ${left(gone ? 0 : Math.round(w.used * 100))}`);
  }
  return parts.join(" · ");
}

// a sign-in from the page: the answer's link and code into the box above the note (an earlier code goes) — of the tab
// on the page now: a snapshot may have drawn it anew while the request went, and the old node is no longer shown
async function signIn(wrap, note, path, body) {
  note.textContent = "…";
  const r = await post(path, body);
  const live = wrap.isConnected ? wrap : document.querySelector(".ac.cx") || wrap;
  const out = live === wrap ? note : live.querySelector(":scope > .ac-note") || note;
  if (r.error) { out.textContent = t("web.codex.failed", { why: r.error }); return; }
  const d = r.data || {};
  if (d.code) { out.textContent = ""; live.querySelector(".cx-code")?.remove(); out.before(codeBox(d.url, d.code, d.account)); return; }
  out.textContent = t("web.codex.done");
}

// the row's state is the core's (codexcli.pool_view: no | lost | busy | active | free) — here only its words
const STATE = { no: () => t("web.codex.row_no_login"), lost: () => t("web.codex.row_lost"),
  busy: (r) => t("web.codex.row_busy", { when: when(r.busy) }), active: () => t("web.codex.row_active"),
  free: () => t("web.codex.row_free") };

function rowState(r) {
  return [r.state, (STATE[r.state] || STATE.free)(r)];
}

function poolRows(wrap, note, pool, now) {
  const list = el("div", "cx-rows");
  for (const r of pool.rows) {
    const [kind, label] = rowState(r);
    const row = el("div", "cx-row");
    row.dataset.account = r.name;
    row.dataset.state = kind;
    const who = el("div", "cx-who");
    who.append(el("b", null, r.name), el("span", "ac-muted", [r.email, r.plan].filter(Boolean).join(" · ") || (r.hub ? t("web.codex.row_hub") : "")));
    const lim = windowsLine(r.limits, now);
    row.append(who, el("span", "cx-state", label), el("span", "cx-lim", lim));
    if (!r.signed || r.lost) {
      const btn = el("button", "btn quiet small", t("web.codex.login"));
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        await signIn(wrap, note, "/api/codex/login", { account: r.name });
        btn.disabled = false;
      });
      row.append(btn);
    }
    list.append(row);
  }
  return list;
}

// "Critic" under the sign-in: off — a line and "Turn on" (the same POST as the Modules card's switch; the snapshot after
// the write redraws the row), on — a short line
function criticRow(on) {
  const row = el("div", "cx-critic");
  row.dataset.on = on ? "1" : "0";
  row.append(el("p", "ac-sum", t(on ? "web.codex.critic_on" : "web.codex.critic_off")));
  if (!on) {
    const btn = el("button", "btn small primary ac-go cx-critic-on", t("web.modules.enable"));
    btn.type = "button";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const r = await post("/api/modules/toggle", { id: "critic", enabled: true });
      if (r.error) { btn.disabled = false; row.append(el("p", "ac-note", r.error)); }
    });
    row.append(btn);
  }
  return row;
}

export function codexSection() {
  const cx = S.snap?.codex || {}, lim = S.snap?.limits || {}, now = Date.now() / 1000;
  const pool = cx.pool && cx.pool.rows && cx.pool.rows.length ? cx.pool : null;
  const wrap = el("div", "ac cx");
  const note = el("p", "ac-note");
  if (pool) {
    wrap.append(el("p", "ac-sum", t(pool.on ? "web.codex.pool_on" : "web.codex.pool_off", { count: pool.rows.length })));
    if (pool.exhausted && pool.exhausted > now) wrap.append(el("p", "ac-stop", t("web.codex.pool_exhausted", { when: when(pool.exhausted) })));
    if (cx.lost) wrap.append(el("p", "ac-stop", t("web.codex.lost", { why: cx.lost.why || "" })));
    wrap.append(poolRows(wrap, note, pool, now));
  } else {
    const head = el("p", "ac-sum");
    head.textContent = cx.login ? t("web.codex.logged_in", { email: cx.login.email || "?", plan: cx.login.plan || "?" })
      : t("web.codex.logged_out");
    wrap.append(head);
    if (cx.lost) wrap.append(el("p", "ac-stop", t("web.codex.lost", { why: cx.lost.why || "" })));
    for (const [name, w] of Object.entries(lim.windows || {})) {
      if (typeof w.used !== "number" || !WIN[name]) continue;
      const gone = w.resets && w.resets < now;
      const pct = gone ? 0 : Math.round(w.used * 100);
      wrap.append(el("p", "cx-lim", `${WIN[name]()}: ${left(pct)}` + (w.resets && !gone ? ` · ${t("web.codex.resets", { when: when(w.resets) })}` : "")));
    }
  }
  const page = cx.page;
  if (page && page.code && !page.result) wrap.append(codeBox(page.url, page.code, page.account));
  else if (page && page.result && !page.result.ok) note.textContent = t("web.codex.failed", { why: page.result.why || "" });
  const acts = el("div", "cx-acts");
  if (!pool) {
    const btn = el("button", "btn quiet ac-go", cx.login ? t("web.codex.logout") : t("web.codex.login"));
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      if (cx.login) {
        note.textContent = "…";
        const r = await post("/api/codex/logout", {});
        note.textContent = r.error ? t("web.codex.failed", { why: r.error }) : t("web.codex.done");
      } else {
        await signIn(wrap, note, "/api/codex/login", {});
      }
      btn.disabled = false;
    });
    acts.append(btn);
  }
  if (!(cx.pool && cx.pool.cut)) {   // cut: this hub's build has no Codex pool (the open flavor) — one sign-in
    const add = el("button", "btn quiet ac-go cx-add", t("web.codex.add"));
    add.addEventListener("click", async () => {
      add.disabled = true;
      await signIn(wrap, note, "/api/codex/add", {});
      add.disabled = false;
    });
    acts.append(add);
  }
  wrap.append(acts, note);
  // the critic (another model checks the important decisions): the Modules card's switch, offered once the hub is in
  // Codex; a snapshot of an older server has no such field — no row
  const signed = (cx.login && !cx.lost) || (pool && pool.rows.some((r) => r.signed && !r.lost));
  if (signed && typeof cx.critic === "boolean") wrap.append(criticRow(cx.critic));
  wrap.append(el("p", "ac-muted", t(pool ? "web.codex.how_pool" : "web.codex.how")));
  return wrap;
}
