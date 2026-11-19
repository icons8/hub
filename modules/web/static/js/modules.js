// Settings → Modules: lists every module (browsers, Tailscale, everything else) with a detailed description of what it
// gives. A module card shows what it gives, whether it is on, and how to turn it on; browsers and voice carry their
// setup inside; optional modules (voice reply) have a toggle.
import { el } from "./dom.js";
import { post } from "./api.js";
import { browsersSection } from "./browsers.js";
import { voiceSection } from "./voicehelp.js";
import { moduleSettings, hasModuleSettings } from "./settingrow.js";
import { t, locale } from "./i18n.js";
import { keepOpen } from "./keepopen.js";

const STATE = { on: t("web.modules.state_on"), part: t("web.modules.state_part"), off: t("web.modules.state_off") };
const SECTIONS = { browsers: browsersSection, voice: voiceSection, stats: statsSection };
let got = null, installed = [], pending = false;
const failed = new Map();   // third-party module -> refusal of the last action (the view is rebuilt after load)
const opened = new Map();   // module -> whether its description (":gives") is expanded (the view is rebuilt with the core snapshot)
const reports = new Map();   // the stats report shown last: a rebuilt block shows it at once
const asking = new Map();   // stats report -> its request in flight

function card(m) {
  const c = el("section", "module-card");
  c.dataset.state = m.state;
  const head = el("div", "module-head");
  head.append(el("h3", null, m.title), el("span", "pill", STATE[m.state] || m.state));
  if (m.core) head.append(el("span", "pill module-core", t("web.modules.core")));   // no switch: the hub needs it to work
  if (m.toggle) head.append(toggle(m));
  c.append(head, ...(m.gives ? [gives(m)] : []));
  if (m.detail) c.append(el("p", "hint", m.detail));
  if (m.command) {  // which command it runs — display only: changed in the hub settings, not from the page
    const cmd = el("code", "module-cmd", m.command);
    cmd.title = t("web.modules.command_title");
    c.append(el("p", "hint", t("web.modules.command")), cmd);
  }
  if (m.how && m.state !== "on" && !(m.toggle && m.enabled)) c.append(el("p", "hint", t("web.modules.how", { how: m.how })));
  const own = moduleSettings(m.id);   // the module's settings live here too (the core serves them only while the module is on)
  if (own) {
    const det = el("details");
    keepOpen(det, m.id + ":set");
    det.append(el("summary", null, t("web.settings.tab.general")), own);
    c.append(det);
  }
  if (m.section && SECTIONS[m.section]) {
    const det = el("details");
    keepOpen(det, m.id, m.state !== "on");
    det.append(el("summary", null, t("web.modules.setup")), SECTIONS[m.section]());
    c.append(det);
  }
  return c;
}

// what the module gives — in two lines; a click or Enter expands it (selecting text with the mouse is not a click)
function gives(m) {
  const p = el("p", "module-gives" + (opened.get(m.id + ":gives") ? " open" : ""), m.gives);
  p.tabIndex = 0;
  p.setAttribute("role", "button");
  const flip = () => {
    const on = !p.classList.contains("open");
    p.classList.toggle("open", on);
    p.setAttribute("aria-expanded", String(on));
    opened.set(m.id + ":gives", on);
  };
  p.setAttribute("aria-expanded", String(p.classList.contains("open")));
  p.addEventListener("click", () => { if (!String(getSelection())) flip(); });
  p.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
  return p;
}

// statistics (telemetry settings): what will be sent and what was sent — the same text as hub stats and
// hub stats log; loaded when expanded (the report is built from the journal — takes seconds)
function report(what) {   // one request per `what` at a time: blocks are rebuilt with every snapshot while it runs
  if (!asking.has(what)) {
    asking.set(what, fetch(`/api/stats?what=${what}`).then((r) => r.json()).then((j) => j.text || j.error || "")
      .catch(() => t("web.modules.stats_offline"))
      .then((text) => { reports.set(what, text); asking.delete(what); return text; }));
  }
  return asking.get(what);
}

function statsSection() {
  const wrap = el("div", "stats-peek");
  for (const [what, title] of [["next", t("web.modules.stats_next")], ["sent", t("web.modules.stats_sent")]]) {
    const d = el("details"), pre = el("pre", null, reports.get(what) ?? t("web.modules.loading"));
    keepOpen(d, "stats:" + what);
    d.append(el("summary", null, title), pre);
    const ask = () => {
      pre.textContent = t("web.modules.loading");
      report(what).then((text) => { pre.textContent = text; });
    };
    let seen = d.open;   // a block restored open after a rebuild does not start a request: it joins the one under way
    d.addEventListener("toggle", () => {
      if (d.open === seen) return;
      seen = d.open;
      if (seen) ask();
    });
    if (d.open && (asking.has(what) || !reports.has(what))) ask();
    wrap.append(d);
  }
  return wrap;
}

function toggle(m) {
  const b = el("button", m.enabled ? "btn quiet small module-toggle" : "btn small primary module-toggle",
    m.enabled ? t("web.modules.disable") : t("web.modules.enable"));
  b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true;
    const r = await post("/api/modules/toggle", { id: m.id, enabled: !m.enabled });
    if (r.error) { b.disabled = false; b.title = r.error; return; }
    load();
  });
  return b;
}

function load() {
  pending = true;
  // on failure the previous list stays; 503 (the page is still reading the journal) while there is no list — ask again in 3 s
  return fetch("/api/modules").then((r) => { if (r.status >= 400) throw r.status; return r.json(); }).then((j) => {
    pending = false;
    const fresh = JSON.stringify([j.modules, j.installed || []]) !== JSON.stringify([got, installed]);
    got = j.modules;
    installed = j.installed || [];
    if (fresh) window.dispatchEvent(new Event("hub-modules"));
    return fresh;
  }).catch((status) => {
    pending = false;
    // redraws the tab if it is open; if the list already exists, do not redraw (card fields would lose input)
    if (status === 503 && !got) setTimeout(() => window.dispatchEvent(new Event("hub-modules")), 3000);
    return false;
  });
}

export function modulesSection() {
  const wrap = el("div", "modules");
  wrap.append(el("p", "hint", t("web.modules.intro")));
  // working core parts without a toggle or settings go in one collapsed row; disabled and faulty ones stay in view
  const quiet = (m) => m.state === "on" && !m.toggle && !m.section && !hasModuleSettings(m.id);
  if (got) {
    const always = got.filter(quiet), det = el("details", "set-more");
    keepOpen(det, ":quiet");
    det.append(el("summary", null, t("web.modules.quiet", { count: always.length })), ...always.map(card));
    wrap.append(...got.filter((m) => !quiet(m)).map(card), ...(always.length ? [det] : []), thirdParty());
  }
  else wrap.append(el("p", "hint", t("web.modules.loading")));
  if (!pending) load();   // fresh data on each opening of the tab; core snapshots do not trigger it
  return wrap;
}

// Third-party modules (Module API, installed with `hub module add`) are shown, each marked as third-party: the same
// rows as `hub modules`, with a badge, source, permissions and buttons
const THIRD_STATE = { ready: "on", static: "on", starting: "wait", disabled: "off", suspended: "bad", stopped: "bad", blocked: "bad" };
const CAPS = {
  "launch.env": t("web.modules.cap_launch_env"), cli: t("web.modules.cap_cli"), doctor: t("web.modules.cap_doctor"),
  exec: t("web.modules.cap_exec"), fs: t("web.modules.cap_fs"), net: t("web.modules.cap_net"), http: t("web.modules.cap_http"),
  prompt: t("web.modules.cap_prompt"), "core.write": t("web.modules.cap_core_write"),
  "voice.transcribe": t("web.modules.cap_voice_transcribe"), sessions: t("web.modules.cap_sessions"),
  accounts: t("web.modules.cap_accounts"), messages: t("web.modules.cap_messages"), model: t("web.modules.cap_model"),
  profile: t("web.modules.cap_profile"),
  speech: t("web.modules.cap_speech"), stats: t("web.modules.cap_stats"),
  "bus.transport": t("web.modules.cap_bus_transport"), "task.admit": t("web.modules.cap_task_admit"),
  "queue.pause": t("web.modules.cap_queue_pause"), "message.rush": t("web.modules.cap_message_rush"),
};
const THIRD_LABEL = {
  ready: t("web.modules.third_ready"), static: t("web.modules.third_static"), starting: t("web.modules.third_starting"), disabled: t("web.modules.third_disabled"),
  suspended: t("web.modules.third_suspended"), stopped: t("web.modules.third_stopped"), blocked: t("web.modules.third_blocked"),
};
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });

function thirdParty() {
  const sec = el("section", "third-party-list");
  sec.append(el("h3", "day", t("web.modules.third_title")), el("p", "hint", t("web.modules.third_intro")),
    ...installed.map(thirdCard));
  if (!installed.length) sec.append(el("p", "hint", t("web.modules.third_none")));
  const cmd = el("code", "module-cmd", t("web.modules.third_cmd"));
  sec.append(el("p", "hint", t("web.modules.third_how")), cmd);
  return sec;
}

function thirdCard(m) {
  const c = el("section", "module-card third-party");
  c.dataset.state = THIRD_STATE[m.state] || "bad";
  c.dataset.name = m.name;
  const head = el("div", "module-head");
  const badge = el("span", "pill third-badge", t("web.modules.third_badge"));
  badge.title = t("web.modules.third_badge_title");
  head.append(el("h3", null, m.name), el("span", "module-ver", m.version), badge,
    el("span", "pill module-state", THIRD_LABEL[m.state] || m.state), act(m, "enable", m.enabled ? t("web.modules.disable") : t("web.modules.enable"),
      { enabled: !m.enabled, embed: !!m.embed }, m.enabled ? "btn quiet small module-toggle" : "btn small primary module-toggle"));
  c.append(head);
  if (m.description) c.append(el("p", null, m.description));
  const from = m.origin === "git"
    ? t(m.ref ? "web.modules.third_from_git_ref" : "web.modules.third_from_git", { source: m.source, ref: m.ref, commit: (m.commit || "").slice(0, 12) })
    : m.embed ? t("web.modules.third_from_embed", { embed: m.embed, source: m.source })   // the build's code (core/embed.py)
    : t("web.modules.third_from_path", { source: m.source });
  const where = el("p", "hint module-from", from);
  where.append(el("span", null, " · " + t("web.modules.third_api", { api: m.api || "—" })));
  if (m.trusted) where.append(el("span", null, " · " + t("web.modules.third_trusted")));
  c.append(where);
  const why = m.state === "suspended" ? t("web.modules.third_why_suspended", { time: m.until ? hhmm(m.until) : "?", failure: m.failure })
    : m.state === "stopped" ? t("web.modules.third_why_stopped", { failure: m.failure })
    : m.state === "blocked" ? t("web.modules.third_why_blocked", { reason: m.reason }) : "";
  if (why || m.halt !== "ok") {   // the reason and the button that clears it — on one row
    const row = el("div", "module-row");
    row.append(...(why ? [el("p", "hint warn module-why", why)] : []));
    if (m.halt !== "ok") row.append(act(m, "reset", m.halt === "suspended" ? t("web.modules.third_reset_pause") : t("web.modules.third_reset_stop"), {}, "btn small"));
    c.append(row);
  }
  if (m.core_write) {   // the right to write to core state — made prominent, ahead of the permissions list
    const row = el("div", "module-row"), w = el("p", "hint core-written", t("web.modules.third_core_write", { name: m.name }));
    const done = m.core_written;
    if (done) w.append(el("span", null, " " + t("web.modules.third_core_written", { count: done.count || 0 })));
    row.append(w);
    if (done && !done.forgot) row.append(act(m, "forget", t("web.modules.third_forget"), {}, "btn quiet small"));
    c.append(row);
  }
  if (m.model) {   // model calls on the hub accounts spend the owner's limits: the owner allows it with the button here
    const row = el("div", "module-row model-right");
    row.append(el("p", "hint" + (m.model_allowed ? "" : " warn"), t(m.model_allowed ? "web.modules.third_model_on" : "web.modules.third_model")),
      act(m, "model", m.model_allowed ? t("web.modules.model_deny") : t("web.modules.model_allow"), { allowed: !m.model_allowed },
        m.model_allowed ? "btn quiet small" : "btn small"));
    c.append(row);
  }
  const caps = el("p", "module-caps");
  caps.append(el("span", "hint", t("web.modules.third_rights")));
  for (const cap of m.capabilities) caps.append(el("span", "cap" + (cap === "core.write" || cap === "model" ? " danger" : ""), CAPS[cap] || cap));
  if (!m.capabilities.length) caps.append(el("span", "hint", t(m.state === "static" ? "web.modules.third_rights_static" : "web.modules.third_rights_none")));
  c.append(caps);
  if (m.dev) c.append(el("p", "hint", t("web.modules.third_dev")));
  if (failed.has(m.name)) c.append(el("p", "hint warn", failed.get(m.name)));
  return c;
}

function act(m, what, label, extra, cls) {
  const b = el("button", cls, label);
  b.type = "button";
  b.dataset.act = what;
  b.addEventListener("click", async () => {
    b.disabled = true;
    const r = await post(`/api/modules/${what}`, { name: m.name, ...extra });
    if (r.error) failed.set(m.name, r.error);
    else failed.delete(m.name);
    if (!(await load())) window.dispatchEvent(new Event("hub-modules"));   // even without new data: show the refusal, restore the button
  });
  return b;
}
