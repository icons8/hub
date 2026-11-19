// "Skills and MCP" of a session (spec d2547143): what its process loaded and what it really used. The owner was sure the
// sessions used all their skills, and they did not; the page showed only what the core granted. #/kit/<session>, from
// the session's "More" menu. The data is GET /api/session/kit (core/uses.py): the set the engine reported, the live MCP
// statuses, the uses of the session and of its executors.
import { dayTime, el } from "./dom.js";
import { S, sessionName } from "./state.js";
import { t } from "./i18n.js";

const PERIODS = [["7", "web.kit.days_7"], ["30", "web.kit.days_30"], ["all", "web.kit.days_all"]];
const DAYS = "hub.kit.days";   // the chosen period: the same for every session
const FRESH = 30000;           // an answer is asked again after this while the view is open
const RETRY = 2000;            // the hub has not read its journal yet: ask again after this
let got = null, gotFor = null, gotAt = 0, asking = 0;

const period = () => (PERIODS.some(([k]) => k === localStorage.getItem(DAYS)) ? localStorage.getItem(DAYS) : "7");

function load(name) {
  const key = `${name}|${period()}`;
  if (key === gotFor && Date.now() - gotAt < FRESH) return;
  gotFor = key;
  gotAt = Date.now();
  const ask = ++asking;   // only the latest request counts
  fetch(`/api/session/kit?session=${encodeURIComponent(name)}&days=${period()}`, { cache: "no-store" })
    .then(async (r) => ({ status: r.status, body: r.ok ? await r.json() : null }))
    .then(({ status, body }) => {
      if (ask !== asking) return;
      if (status === 503) {   // the hub is still reading its journal after a start: "loading", asked again in a moment
        got = null;
        gotFor = null;
        setTimeout(() => window.dispatchEvent(new Event("hub-kit")), RETRY);
        return;
      }
      got = body || { failed: status };
      window.dispatchEvent(new Event("hub-kit"));
    })
    .catch(() => { if (ask === asking) { got = { failed: 0 }; gotAt = Date.now() - FRESH + 5000; window.dispatchEvent(new Event("hub-kit")); } });
}

const SOURCE = { plugin: "web.kit.src_plugin", repo: "web.kit.src_repo", "built-in": "web.kit.src_builtin",
  user: "web.kit.src_user" };
// a repo skill's repo: the folder above `.claude/skills` (a working copy: the repo it was made from, not the copy)
const repoOf = (path) => {
  const dir = String(path || "").replace(/\\/g, "/").split("/.claude/skills/")[0].replace(/\/\.claude\/worktrees\/[^/]+$/, "");
  return dir !== String(path || "").replace(/\\/g, "/") ? dir.split("/").filter(Boolean).pop() || "" : "";
};
const source = (r) => (r.source === "plugin" && r.plugin ? t("web.kit.src_plugin_named", { name: r.plugin })
  : r.source === "repo" && repoOf(r.path) ? t("web.kit.src_repo_named", { name: repoOf(r.path) })
  : SOURCE[r.source] ? t(SOURCE[r.source]) : r.source || "");
const calls = (u) => (u ? u.tool + u.command + u.file : 0);
const none = (n) => (n ? String(n) : "—");

// the tooltip of a count: how it was used
function how(u) {
  if (!u) return "";
  return [u.tool && t("web.kit.how_tool", { count: u.tool }), u.command && t("web.kit.how_command", { count: u.command }),
    u.file && t("web.kit.how_file", { count: u.file }), u.failed && t("web.kit.how_failed", { count: u.failed }),
    u.unknown && t("web.kit.how_unknown", { count: u.unknown }), u.unverified && t("web.kit.how_unverified")]
    .filter(Boolean).join("\n");
}

function lastText(own, ex, d) {
  const last = Math.max(own?.last || 0, ex?.last || 0);
  if (last) return dayTime(last);
  return d.complete ? t("web.kit.never") : "—";   // the older journals are still being read: not "never", and the note above says why
}

function row(name, sub, own, ex, d, tags = [], title = "") {
  const li = el("li", "kit-row" + (calls(own) + calls(ex) ? "" : " unused"));
  li.dataset.key = name;
  const what = el("div", "kit-what");
  what.append(el("span", "kit-name", name));
  if (sub) {
    const note = el("span", "kit-sub", sub);
    if (title) note.title = title;
    what.append(note);
  }
  for (const [cls, text, title] of tags) {
    const tag = el("span", "kit-tag " + cls, text);
    if (title) tag.title = title;
    what.append(tag);
  }
  const n = el("span", "kit-n", d.used.executors_count ? `${none(calls(own))} · ${none(calls(ex))}` : none(calls(own)));
  n.title = [how(own), d.used.executors_count && ex ? t("web.kit.executors_how") + "\n" + how(ex) : ""].filter(Boolean).join("\n\n");
  const last = el("span", "kit-last", lastText(own, ex, d));
  if (!d.complete && !calls(own) && !calls(ex) && !(own?.last || ex?.last)) last.title = t("web.kit.reading", { when: dayTime(d.read_from || Date.now() / 1000) });
  li.append(what, n, last);
  return li;
}

function skills(d) {
  const kit = d.kit || { skills: [] }, own = d.used.own, ex = d.used.executors;
  const used = (k) => calls(own[k]) + calls(ex[k]);
  const rows = (kit.skills || []).map((r) => ({ r, key: "skill:" + r.name }));
  const have = new Set(rows.map((x) => x.key));
  // uses of what is not in the set now (switched off, an earlier process): still shown
  for (const k of new Set([...Object.keys(own), ...Object.keys(ex)])) {
    if (k.startsWith("skill:") && !have.has(k)) rows.push({ r: { name: k.slice(6), gone: true }, key: k });
  }
  rows.sort((a, b) => used(b.key) - used(a.key) || a.r.name.localeCompare(b.r.name));
  const ul = el("ul", "kit-list"), builtin = el("ul", "kit-list"), gone = el("ul", "kit-list");
  for (const { r, key } of rows) {
    const tags = [];
    if (own[key]?.this_conversation) tags.push(["on", t("web.kit.this_conversation"), t("web.kit.this_conversation_title")]);
    if (r.in_prompt === false) tags.push(["warn", t("web.kit.not_in_prompt"), t("web.kit.not_in_prompt_title")]);
    if (r.gone) tags.push(["", t("web.kit.not_in_set")]);
    const li = row(r.name, r.gone ? "" : source(r), own[key], ex[key], d, tags, r.path || "");
    // a skill that is not in the set and was not called in the period: the history, apart from the set
    (r.gone && !used(key) ? gone : r.source === "built-in" && !used(key) ? builtin : ul).append(li);
  }
  for (const e of kit.plugin_errors || []) {
    const li = el("li", "kit-row bad");
    li.append(el("div", "kit-what", t("web.kit.plugin_failed", { name: e.plugin, error: e.text || e.type })));
    ul.append(li);
  }
  const box = el("section", "kit-part");
  box.append(el("h3", "day", t("web.kit.skills", { count: (kit.skills || []).length })), ul);
  if (builtin.children.length) {
    const fold = el("details", "kit-fold");
    fold.append(el("summary", null, t("web.kit.builtin", { count: builtin.children.length })), builtin);
    box.append(fold);
  }
  if (gone.children.length) {
    const fold = el("details", "kit-fold kit-gone");
    fold.append(el("summary", null, t("web.kit.gone", { count: gone.children.length })), gone);
    box.append(fold);
  }
  return box;
}

const STATUS = { connected: "web.kit.mcp_connected", failed: "web.kit.mcp_failed", "needs-auth": "web.kit.mcp_auth",
  disabled: "web.kit.mcp_disabled", pending: "web.kit.mcp_pending" };

function servers(d, live) {
  const own = d.used.own, ex = d.used.executors;
  const names = new Set([...Object.keys(d.mcp_status || {}), ...((d.kit || {}).mcp || []).map((m) => m.name)]);
  for (const k of [...Object.keys(own), ...Object.keys(ex)]) if (k.startsWith("mcp:")) names.add(k.slice(4).split("/")[0]);
  const ul = el("ul", "kit-list");
  const tools = (server) => [...new Set([...Object.keys(own), ...Object.keys(ex)])].filter((k) => k.startsWith(`mcp:${server}/`));
  const sum = (box, keys) => keys.reduce((n, k) => n + calls(box[k]), 0);
  for (const server of [...names].sort((a, b) => sum(own, tools(b)) + sum(ex, tools(b)) - sum(own, tools(a)) - sum(ex, tools(a)) || a.localeCompare(b))) {
    const st = (d.mcp_status || {})[server], count = ((d.kit || {}).mcp || []).find((m) => m.name === server)?.tools;
    // a session with no process now (asleep, parked): the status is the last process's
    const word = st ? t(STATUS[st] || "web.kit.mcp_pending") : t("web.kit.mcp_not_now");
    const sub = [st && !live ? t("web.kit.mcp_last", { status: word }) : word,
      count != null ? t("web.kit.mcp_tools", { count }) : ""].filter(Boolean).join(" · ");
    const keys = tools(server);
    const li = row(server, sub, keys.length ? { tool: sum(own, keys), command: 0, file: 0, last: Math.max(0, ...keys.map((k) => own[k]?.last || 0)) } : null,
      keys.length ? { tool: sum(ex, keys), command: 0, file: 0, last: Math.max(0, ...keys.map((k) => ex[k]?.last || 0)) } : null, d,
      st && st !== "connected" ? [["warn", t(STATUS[st] || "web.kit.mcp_pending")]] : []);
    li.classList.add("kit-server");
    ul.append(li);
    keys.sort((a, b) => calls(own[b]) + calls(ex[b]) - calls(own[a]) - calls(ex[a]));
    for (const k of keys) {
      const tool = row(k.slice(5 + server.length), "", own[k], ex[k], d);
      tool.classList.add("kit-tool");
      const args = { ...(ex[k]?.args || {}) };
      for (const [a, n] of Object.entries(own[k]?.args || {})) args[a] = (args[a] || 0) + n;
      if (Object.keys(args).length) {
        const more = (own[k]?.args_more || 0) + (ex[k]?.args_more || 0);
        tool.querySelector(".kit-what").append(el("span", "kit-args", Object.entries(args).sort((a, b) => b[1] - a[1])
          .map(([a, n]) => `${a} ×${n}`).join(" · ") + (more ? " · " + t("web.kit.args_more", { count: more }) : "")));
      }
      ul.append(tool);
    }
  }
  const box = el("section", "kit-part");
  box.append(el("h3", "day", t("web.kit.mcp", { count: names.size })), ul, el("p", "hint", t("web.kit.mcp_inside")));
  return box;
}

export function kitView() {
  const name = S.kitFor, s = (S.snap.sessions || []).find((x) => x.name === name);
  if (s) load(name);
  const wrap = el("div", "sched kit");
  const h = el("div", "head kit-head");
  const back = el("button", "btn quiet back", t("web.kit.back"));
  back.type = "button";
  back.addEventListener("click", () => { location.hash = "#/s/" + encodeURIComponent(name); });
  h.append(back, el("h2", "page-title", t("web.kit.title", { name: sessionName(name) })));
  const seg = el("div", "seg kit-period");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", t("web.kit.period"));
  for (const [key, label] of PERIODS) {
    const b = el("button", null, t(label));
    b.type = "button";
    b.setAttribute("aria-pressed", String(period() === key));
    b.addEventListener("click", () => { localStorage.setItem(DAYS, key); load(name); window.dispatchEvent(new Event("hub-kit")); });
    seg.append(b);
  }
  h.append(el("span", "spacer"), seg);
  const f = el("div", "feed"), inner = el("div", "feed-inner kit-body");
  const d = got && gotFor === `${name}|${period()}` ? got : null;
  if (!s) inner.append(el("p", "hint", t("web.main.no_such", { name: sessionName(name) })));
  else if (!d) inner.append(el("p", "hint", t("web.kit.loading")));
  else if (d.failed != null) inner.append(el("p", "hint warn", d.failed === 404 ? t("web.kit.old_server") : t("web.kit.failed")));
  else {
    const kit = d.kit;
    const notes = [];
    if (kit) notes.push(t("web.kit.set_at", { when: dayTime(kit.at) })
      + (kit.listed ? " · " + t("web.kit.listed", { count: kit.listed.in_prompt, total: kit.listed.total }) : "")
      + (Number.isInteger(kit.listed?.tokens) ? " · " + t("web.kit.list_tokens", { count: kit.listed.tokens }) : ""));
    else notes.push(t("web.kit.no_set"));
    // the denominator of the counts below: how much happened in the period (an older server does not say it)
    if (d.volume) notes.push(t("web.kit.volume", d.volume.own)
      + (d.used.executors_count ? "; " + t("web.kit.volume_executors", d.volume.executors) : ""));
    if (!d.complete) notes.push(t("web.kit.reading", { when: dayTime(d.read_from || Date.now() / 1000) }));
    const since = period() === "all" ? 0 : Date.now() / 1000 - Number(period()) * 86400;
    if (d.files_from && d.files_from > since) notes.push(t("web.kit.files_from", { when: dayTime(d.files_from) }));
    if ((s.tools || []).length) notes.push(t("web.kit.granted", { list: s.tools.join(", ") }));
    // one block: the theme spaces the view's children apart, the notes are read together
    const head = el("div", "kit-notes");
    head.append(...notes.map((n) => el("div", "hint kit-note", n)));
    if (d.lead) {
      const list = d.lead.missing.slice(0, 4).join(", "), more = d.lead.missing.length - Math.min(4, d.lead.missing.length);
      head.append(el("div", "hint warn kit-note", t("web.kit.lead_missing", { name: sessionName(d.lead.name), list })
        + (more ? " " + t("web.kit.and_more", { count: more }) : "")));
    }
    if (d.used.executors_count) head.append(el("div", "hint kit-cols", t("web.kit.columns", { count: d.used.executors_count })));
    inner.append(head);
    inner.append(skills(d), servers(d, !["asleep", "failed"].includes(s.status) && !s.parked));
  }
  f.append(inner);
  wrap.append(h, f);
  return wrap;
}
