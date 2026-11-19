// The "MCP waits for a login" band at the top of the page (snapshot mcp_waits and mcp_logins, core/mcpwatch.py): a row per
// waiting server — since when, which sessions need it, and "Sign in" (POST /api/mcp/login: hubd signs it in through a live
// session). The link opens in a new tab; the browser comes back to the hub by itself when it runs on the hub's machine,
// otherwise (a phone, another computer) the address from its bar goes back through the field (POST /api/mcp/callback).
// A server from hub.json mcp_login_confirm (money, production) — after a confirmation. Model and look — PR #325.
import { el, icon } from "./dom.js";
import { post } from "./api.js";
import { t, locale } from "./i18n.js";
import { confirmDlg } from "./dialogs.js";

const LIFE = 290;       // s: an attempt lives this long in Claude Code (core/mcpwatch.LIFE)
const ASK_LINK = 2;     // s: a link not there yet (the old way takes up to a minute) is asked for again this often
const SHOW_DONE = 60;   // s: "signed in" stays this long after the server stopped waiting

export const mcpBar = el("section", "mcpwait");
mcpBar.setAttribute("aria-label", t("web.mcp.label"));
mcpBar.hidden = true;

const mine = {};     // row key -> {busy, attempt, url, error}: what this tab knows before the core does
const drafts = {};   // row key -> the address being typed (kept through redraws)
let rows = [], logins = [], painted = "", timer = null;

const keyOf = (r) => `${r.server}\n${r.fp ?? ""}`;

function when(ts) {
  const d = new Date(ts * 1000), hm = d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === new Date().toDateString()) return hm;
  return `${d.toLocaleDateString(locale, { day: "numeric", month: "short" })}, ${hm}`;
}

// the latest attempt of this server at this address (the snapshot has the latest first)
const attemptOf = (row) => logins.find((a) => a.server === row.server && (a.fp ?? "") === (row.fp ?? ""));

function stateOf(row, now) {
  const m = mine[keyOf(row)] || {}, a = attemptOf(row);
  if (m.busy) return { kind: "opening" };
  if (m.error && (!a || a.id === m.attempt || a.ts < m.at)) return { kind: "idle", error: m.error };
  if (!a) return { kind: "idle" };
  const live = now - a.ts < LIFE;
  if (a.state === "done") return { kind: "done" };
  if (a.state === "failed") return { kind: "idle", error: a.error || t("web.mcp.failed"), again: true };
  if (!live) return { kind: "idle", error: t("web.mcp.expired"), again: true };
  if (a.state === "asked") return { kind: "opening" };
  if (a.state === "sent") return { kind: "checking" };
  const url = m.attempt === a.id ? m.url : null;
  if (!url && !m.fetching && now - (m.asked || 0) >= ASK_LINK) fetchLink(row, a.id);
  return { kind: "link", url, error: a.error, attempt: a.id };
}

// a link begun in another tab, before a reload, or slow (the old way): asked again every ASK_LINK until it is there
async function fetchLink(row, attempt) {
  const k = keyOf(row);
  mine[k] = { ...mine[k], fetching: true, asked: Date.now() / 1000 };
  const r = await post("/api/mcp/link", { attempt });
  if (!mine[k]?.fetching) return;   // a new press or the row went away meanwhile: the answer is stale
  mine[k] = r.data?.url ? { attempt, url: r.data.url } : { ...mine[k], fetching: false };
  paint();
}

async function login(row, confirmed) {
  const k = keyOf(row);
  mine[k] = { busy: true };
  paint();
  const r = await post("/api/mcp/login", { server: row.server, fp: row.fp ?? "", ...(confirmed ? { confirmed: true } : {}) });
  const why = r.data?.why ? `: ${r.data.why}` : "";
  if (r.error) mine[k] = { error: r.error + why, attempt: r.data?.attempt, at: Date.now() / 1000 };
  else mine[k] = { attempt: r.data?.attempt, url: r.data?.url || null, asked: Date.now() / 1000 };
  paint();
}

// the cross: the core keeps the mark (every device sees the row closed); the row goes away with the next snapshot
async function dismiss(row) {
  const k = keyOf(row), r = await post("/api/mcp/dismiss", { server: row.server, fp: row.fp ?? "" });
  // its own field: the sign-in's state (an open link, "signed in") stays as it was; a press that went through forgets it
  const { closeError, ...rest } = mine[k] || {};
  mine[k] = r.error ? { ...rest, closeError: r.error + (r.data?.why ? `: ${r.data.why}` : "") } : rest;
  paint();
}

function start(row) {
  if (!row.confirm) return login(row, false);
  confirmDlg(t("web.mcp.confirm_title", { server: row.server }), t("web.mcp.confirm_text", { server: row.server }),
    t("web.mcp.login"), () => login(row, true));
}

async function send(row, attempt, url) {
  const k = keyOf(row);
  mine[k] = { ...mine[k], sending: true };
  paint();
  const r = await post("/api/mcp/callback", { attempt, url });
  mine[k] = { ...mine[k], sending: false, sendError: r.error || null };
  if (!r.error) delete drafts[k];
  paint();
}

function rowNode(row, st) {
  const r = rowBody(row, st), why = mine[keyOf(row)]?.closeError;
  if (row.sessions && why) {
    const e = el("span", "mcpwait-err", t("web.mcp.failed"));
    e.title = why;
    e.setAttribute("role", "status");
    e.setAttribute("aria-label", `${t("web.mcp.failed")}: ${why}`);
    r.append(e);
  }
  if (row.sessions) r.append(cross(row));   // a row "signed in" (no sessions any more) goes away by itself
  return r;
}

function cross(row) {
  const x = el("button", "mcpwait-x");
  x.type = "button";
  x.title = t("web.mcp.dismiss_hint");
  x.setAttribute("aria-label", t("web.mcp.dismiss"));
  x.append(icon("close"));
  x.addEventListener("click", () => dismiss(row));
  return x;
}

function rowBody(row, st) {
  const k = keyOf(row), r = el("div", "mcpwait-row");
  r.dataset.server = row.server;
  r.dataset.key = k;
  const ic = el("span", "mcpwait-ic");
  ic.append(icon("key"));
  const tx = el("span", "mcpwait-t");
  tx.append(el("b", null, row.server), row.host ? el("span", "mcpwait-who", ` ${row.host}`) : "");
  if (row.sessions) {
    tx.append(" ", t("web.mcp.since", { when: when(row.since) }), " · ",
      t("web.mcp.need", { count: row.sessions.length }), ": ", el("span", "mcpwait-who", row.sessions.join(", ")));
  }
  tx.title = tx.textContent;
  r.append(ic, tx);
  if (st.kind === "opening" || st.kind === "checking" || st.kind === "done") {
    const w = el("span", st.kind === "done" ? "mcpwait-ok" : "mcpwait-wait",
      t({ opening: "web.mcp.opening", checking: "web.mcp.checking", done: "web.mcp.done" }[st.kind]));
    w.setAttribute("role", "status");
    r.append(w);
    return r;
  }
  if (st.error) {
    const e = el("span", "mcpwait-err", t("web.mcp.failed"));
    e.title = st.error;
    e.setAttribute("role", "status");
    e.setAttribute("aria-label", `${t("web.mcp.failed")}: ${st.error}`);
    r.append(e);
  }
  if (st.kind === "link") {
    const m = mine[k] || {};
    const f = el("form", "mcpwait-link");
    if (st.url) {
      const a = el("a", "btn small primary", t("web.mcp.open"));
      a.href = st.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      f.append(a);
    } else {
      f.append(el("span", "mcpwait-wait", t("web.mcp.opening")));
    }
    const inp = el("input");
    inp.type = "url";
    inp.name = "redirect";
    inp.required = true;
    inp.placeholder = t("web.mcp.redirect");
    inp.title = t("web.mcp.redirect_hint");
    inp.setAttribute("aria-label", t("web.mcp.redirect_hint"));
    inp.value = drafts[k] || "";
    inp.addEventListener("input", () => { drafts[k] = inp.value; });
    const go = el("button", "btn small", t("web.mcp.send"));
    go.disabled = !!m.sending;
    f.addEventListener("submit", (e) => { e.preventDefault(); send(row, st.attempt, inp.value.trim()); });
    f.append(inp, go);
    if (m.sendError) {
      const e = el("span", "mcpwait-err", t("web.mcp.failed"));
      e.title = m.sendError;
      f.append(e);
    }
    r.append(f);
    return r;
  }
  const b = el("button", "btn small primary", t(st.again ? "web.mcp.again" : "web.mcp.login"));
  b.type = "button";
  b.addEventListener("click", () => start(row));
  r.append(b);
  return r;
}

function paint() {
  clearTimeout(timer);
  timer = null;
  const now = Date.now() / 1000;
  // a server signed in a moment ago no longer waits: its row says so for SHOW_DONE
  const waiting = new Set(rows.map(keyOf));
  const done = logins.filter((a, i) => a.state === "done" && now - a.at < SHOW_DONE && !waiting.has(keyOf(a))
    && logins.findIndex((b) => keyOf(b) === keyOf(a)) === i);
  const all = [...rows, ...done.map((a) => ({ server: a.server, fp: a.fp, host: a.host }))];
  const states = all.map((row) => stateOf(row, now));
  mcpBar.hidden = !all.length;
  const model = JSON.stringify([all, states, Object.values(mine).map((m) => [m.sending, m.sendError, m.closeError])]);
  if (model !== painted) {   // snapshots come every second: a field in the band keeps its focus and caret
    painted = model;
    const f = mcpBar.contains(document.activeElement) ? document.activeElement : null;
    const at = f?.closest(".mcpwait-row")?.dataset.server, key = f?.closest(".mcpwait-row")?.dataset.key, pos = f?.selectionStart;
    mcpBar.replaceChildren(...all.map((row, i) => rowNode(row, states[i])));
    // the cross is a second button in the row: by its class and in its own row (another address of the server is
    // another row), or the focus would land on "Sign in"
    const cross = f?.classList.contains("mcpwait-x");
    const again = at && mcpBar.querySelector(cross ? `.mcpwait-row[data-key="${CSS.escape(key)}"] .mcpwait-x`
      : `.mcpwait-row[data-server="${CSS.escape(at)}"] ${f.tagName.toLowerCase()}`);
    if (again) {
      again.focus({ preventScroll: true });
      if (pos != null && again.setSelectionRange) again.setSelectionRange(pos, pos);
    }
  }
  // the states that end by time: an attempt that runs out, "signed in" that goes away
  if (states.some((s) => s.kind !== "idle") || done.length) timer = setTimeout(paint, 2000);
}

// every render: the core's lists; a server that no longer waits (and was not just signed in) forgets this tab's state
export function renderMcp(waits, attempts) {
  rows = Array.isArray(waits) ? waits : [];
  logins = Array.isArray(attempts) ? attempts : [];
  const keep = new Set([...rows.map(keyOf), ...logins.map(keyOf)]);
  for (const k of Object.keys(mine)) if (!keep.has(k)) { delete mine[k]; delete drafts[k]; }
  paint();
}
