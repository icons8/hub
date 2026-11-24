// The hub's setup (modules/setup, installer spec §6.1, §9): what takes a new hub to its first answer — Claude Code, the
// sign-in to Claude, Chrome for the agents' browser, the team's modules over GitHub (only where they are in play),
// `hub doctor`. The hub does each step itself in its own process (POST /api/setup/run); the page shows the facts of
// GET /api/setup and gives the person the two things only they can do: sign in in their own browser and paste a code.
// There is no tab for it. While a required item (Claude Code, the sign-in) is red, a band above the page says what holds
// the hub and has the buttons; Settings → About the hub shows a card only for what is not done (the agents' browser, the
// team's modules, a hub check with remarks) and nothing when all is well.
// A page server without /api/setup (an older one, before its restart) has no "setup" in features: no band, no cards.
// a hub entirely on Codex has `codex` and `codex_login` instead of Claude Code
// and its sign-in; the sign-in is Codex's device code (POST /api/codex/login, as "Settings → Codex").
import { el } from "./dom.js";
import { post } from "./api.js";
import { S } from "./state.js";
import { t, lang } from "./i18n.js";
import { copyText } from "./markdown.js";

const FAST = 2000, SLOW = 120000;
const MANUAL = "https://code.claude.com/docs/en/setup";
const CHROME = "https://www.google.com/chrome/";

export const setupBar = el("section", "setupbar");
setupBar.setAttribute("aria-label", t("web.setup.title"));
setupBar.hidden = true;

let again = false;   // a load asked for while one runs (About opened): one more right after it
let st = null, timer = null, busy = false, aboutNode = null, aboutPainted = "", autoDone = false, doctorDone = false, doctorShown = false, stFull = false;
let langAsked = 0;   // when the check was asked again in this page's language
const LANG_WAIT = 30000;   // ms: a check of another language is not shown while the one asked in this page's runs
const drafts = {}, mine = {};   // field drafts and the person's own refusals, kept through redraws
let pressedUntil = 0;   // a step was started here: the page looks every FAST until it shows (a run may not be "running" yet)
let quickUntil = 0;   // "Update" pressed (updates.js says so): the band looks every FAST for a while
addEventListener("hub-update-pressed", () => { quickUntil = Date.now() + 15 * 60000; load(); });

export const setupOn = () => !!S.snap?.features?.includes("setup");
// the app (app spec §4a): its steps run by themselves (modules/setup/auto.py) and the band is their only view: what
// holds the hub, then a failure with its reason and "Retry", then progress (the update's too). A hub from git has the
// band for what holds it (Claude Code, the sign-in) and its steps are the person's buttons
export const appOn = () => setupOn() && !!S.snap?.features?.includes("setup.app");

async function load() {
  clearTimeout(timer);
  timer = null;
  if (!setupOn()) { st = null; paint(); return; }
  if (busy) { again = true; return; }
  busy = true;
  const full = !!aboutNode?.isConnected;
  try {
    const r = await fetch("/api/setup" + (full ? "" : "?full=0"), { headers: { "X-Hub": "1" } });
    if (r.ok) { st = await r.json(); stFull = full; }   // short (not full): only claude and login — the band's round while About is away
  } catch { /* no answer: the next round */ }
  busy = false;
  if (again) { again = false; return load(); }
  if (st?.auto?.length && !autoDone) {   // «Claude Code downloads by itself» (§6 step 7) — once per page
    autoDone = true;
    for (const step of st.auto) run(step);
  }
  const doc = item("doctor");   // «Hub check» runs by itself when About opens and the last one is old (§6.1),
  // and when its rows are words of another language than this page's (a fresh hub checked itself in the machine's)
  if (doc && doc.state !== "running") {
    if (full && doc.stale && !doctorDone) { doctorDone = true; run("doctor"); }
    else if (otherLang(doc) && !langAsked) { langAsked = Date.now(); run("doctor"); }
  }
  paint();
  // a step runs (About shows its card, the band its progress), the app: a step waits for the network, or "Update" was
  // pressed lately (its download is a file of the update process, not in the snapshot)
  const live = (st?.items || []).some((i) => i.state === "running") || Date.now() < pressedUntil
    || (appOn() && (!!st?.update || !!Object.keys(st?.waits || {}).length || Date.now() < quickUntil));
  timer = setTimeout(load, live || (st && !st.ready) ? FAST : SLOW);
}

export function renderSetup() {
  if (!timer && !busy) load();
  paint();
}

// the check's rows are text of the language it ran in (job.lang; older checks name none — taken as they are)
const otherLang = (doc) => !!doc?.job?.lang && doc.job.lang !== lang;
// not shown: a check of another language while this page's is being asked (a page of another language next to this
// one may ask it back: after LANG_WAIT it is shown as it is)
const hiddenCheck = (doc) => otherLang(doc) && (!langAsked || Date.now() - langAsked < LANG_WAIT);

async function run(step) {
  pressedUntil = Date.now() + 15000;
  mine[step] = { busy: true };
  paint();
  const r = await post("/api/setup/run", { step });
  mine[step] = r.error ? { error: r.error } : {};
  load();
}

async function cancel(step) {
  await post("/api/setup/cancel", { step });
  load();
}

async function give(step, text) {
  mine[step] = { busy: true };
  paint();
  const r = await post("/api/setup/input", { step, text });
  mine[step] = r.error ? { error: r.error } : { sent: true };
  if (!r.error) drafts[step] = "";
  load();
}

// «A file for support»: a POST from the page (another site cannot start it with a link), the zip saved by the browser
async function bundle(btn) {
  btn.disabled = true;
  btn.textContent = t("web.setup.bundle_making");
  try {
    const r = await fetch("/api/setup/bundle", { method: "POST", headers: { "Content-Type": "application/json", "X-Hub": "1" }, body: "{}" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || String(r.status));
    const name = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "")?.[1] || "hub-support.zip";
    const a = el("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    btn.textContent = t("web.setup.bundle");
  } catch (e) {   // no answer at all: the page's own words, not the browser's «Failed to fetch»
    btn.textContent = t("web.setup.bundle_failed", { error: e instanceof TypeError ? t("web.api.no_answer") : e.message });
  }
  btn.disabled = false;
}

const item = (id) => (st?.items || []).find((i) => i.id === id);

// ---- the band (app spec §4a): one line for everything the setup and the update do -------------------------------
const MB = (n) => (Number(n) / 1e6).toFixed(0);
const BUSY = ["busy", "dialog", "starting"];   // the sessions a full restart waits for (core/restart.WAITING)

// the "update the hub: limits" band (app spec §7.2, min.json) — slice A5 fills this in: above everything else
function limitsBand() { return null; }

function claudeBand(c, waits, running) {
  const j = c.job || {};
  if (c.state === "running") {
    if (j.step === "claude_upgrade") return { kind: "claude_upgrade", level: "hold", text: t("web.setup.app_upgrade_running", { manager: c.manager || "" }) };
    return { kind: "claude", level: "hold", progress: j.progress, acts: [CODEX_INSTEAD()], text: Array.isArray(j.progress) && j.progress[1]
      ? t("web.setup.app_claude_progress", { done: MB(j.progress[0]), total: MB(j.progress[1]) }) : t("web.setup.app_claude_running") };
  }
  if (c.manager === "npm" || c.manager === "brew") {   // a second install next to theirs is never put: their own update
    const acts = [{ label: t(c.state === "failed" ? "web.setup.again" : "web.setup.app_upgrade"), run: "claude_upgrade", primary: true }, CODEX_INSTEAD()];
    return { kind: "claude_upgrade", level: c.state === "failed" ? "fail" : "hold", acts, why: c.state === "failed" ? reason(j) : "",
      text: t("web.setup.app_upgrade_need", { version: c.version, need: c.need, manager: c.manager === "npm" ? "npm" : "Homebrew" }) };
  }
  // older, in a place the hub cannot tell: the person's choice — unless the release's own copy is coming (core/ownclaude)
  if (c.version && c.manager !== "anthropic" && !c.pending) {
    return { kind: "claude_old", level: c.state === "failed" ? "fail" : "hold", why: c.state === "failed" ? reason(j) : "",
      text: t("web.setup.claude_old", { version: c.version, need: c.need }),
      acts: [{ label: t("web.setup.claude_install"), run: "claude", primary: true }, { label: t("web.setup.manual"), href: MANUAL }, CODEX_INSTEAD()] };
  }
  if (c.found && !c.version && c.state !== "failed") return { kind: "claude_broken", level: "fail",
    text: t("web.setup.app_claude_broken"), acts: [{ label: t("web.setup.manual"), href: MANUAL, primary: true }, CODEX_INSTEAD()] };
  if (waits.claude) return { kind: "claude_net", level: "fail", text: t("web.setup.app_claude_net"), acts: [CODEX_INSTEAD()] };
  if (c.state === "failed") return { kind: "claude_failed", level: "fail", text: t("web.setup.app_claude_failed"), why: reason(j),
    acts: [{ label: t("web.setup.again"), run: "claude", primary: true }, { label: t("web.setup.manual"), href: MANUAL }, CODEX_INSTEAD()] };
  // auto.py is about to start it: no step file yet; one that was cancelled or ended is the person's button
  if (running && !c.job) return { kind: "claude", level: "hold", text: t("web.setup.app_claude_running"), acts: [CODEX_INSTEAD()] };
  // the steps do not run (cancelled, ended, a crash): the person's button, as the tab of a hub from git has it
  return { kind: "claude", level: "hold", text: t("web.setup.claude_todo"),
    acts: [{ label: t("web.setup.claude_install"), run: "claude", primary: true }, { label: t("web.setup.manual"), href: MANUAL }, CODEX_INSTEAD()] };
}

// the browser is on the hub's machine: a request from it, and the machine has a screen (a server opened through an SSH
// tunnel asks from 127.0.0.1 too, but its localhost link would land on the person's computer, a port the tunnel lacks)
const browserIsHere = () => !!st?.local && st.screen !== false;
const loginUrl = (j) => browserIsHere() ? j.local_url || j.url : j.url || j.local_url;

function loginBand(l) {
  const j = l.job || {};
  const url = loginUrl(j);
  if (l.state === "running" && !url) return { kind: "login", level: "hold", text: t("web.setup.login_starting") };
  // no sign-in yet: the engine is chosen here — Claude, or Codex (it writes the hub's host and restarts the core)
  const codex = { label: t("web.setup.app_codex_go"), post: ["/api/setup/host", { host: "codex" }] };
  const band = { kind: "login", level: "hold", text: t(appOn() ? "web.setup.app_choose" : "web.setup.band_login") };
  if (l.state === "running") {
    band.acts = [{ label: t("web.setup.app_login_go"), href: url, primary: true }, codex];
    band.code = !browserIsHere();   // a browser on another machine: Anthropic shows a code to paste here
    if (j.code_sent) band.why = t("web.setup.login_checking");
  } else {
    band.acts = [{ label: t("web.setup.app_login_go"), run: "login", primary: true, open: true }, codex];
    if (l.state === "failed") band.why = j.why === "code" ? t("web.setup.login_bad_code") : j.why === "timeout" ? "" : reason(j);
  }
  return band;
}

// a hub on Codex (the person chose it on the band): Codex CLI from OpenAI's releases, then the device code
const CODEX_RELEASES = "https://github.com/openai/codex/releases";
const CLAUDE_INSTEAD = () => ({ label: t("web.setup.app_claude_instead"), post: ["/api/setup/host", { host: "claude" }] });
// the engine may be chosen from the very start: no need to wait for Claude Code's download to pick Codex
const CODEX_INSTEAD = () => ({ label: t("web.setup.app_codex_go"), post: ["/api/setup/host", { host: "codex" }] });

function codexBand(x, waits, running) {
  const j = x.job || {};
  if (x.state === "running") return { kind: "codex", level: "hold", progress: j.progress, text: Array.isArray(j.progress) && j.progress[1]
    ? t("web.setup.app_codex_progress", { done: MB(j.progress[0]), total: MB(j.progress[1]) }) : t("web.setup.app_codex_running") };
  if (x.version && !x.own) return { kind: "codex_old", level: "hold", text: t("web.setup.app_codex_old", { version: x.version, need: x.need }),
    acts: [{ label: t("web.setup.manual"), href: CODEX_RELEASES, primary: true }, CLAUDE_INSTEAD()] };
  if (x.found && !x.own && !x.version) return { kind: "codex_broken", level: "fail", text: t("web.setup.cx_g_codex_broken"),
    why: x.path || "", acts: [{ label: t("web.setup.manual"), href: CODEX_RELEASES, primary: true }, CLAUDE_INSTEAD()] };
  if (waits.codex) return { kind: "codex_net", level: "fail", text: t("web.setup.app_codex_net"), acts: [CLAUDE_INSTEAD()] };
  if (x.state === "failed") return { kind: "codex_failed", level: "fail", text: t("web.setup.app_codex_failed"), why: reason(j),
    acts: [{ label: t("web.setup.again"), run: "codex", primary: true }, CLAUDE_INSTEAD()] };
  if (running && !x.job) return { kind: "codex", level: "hold", text: t("web.setup.app_codex_running") };
  return { kind: "codex", level: "hold", text: t("web.setup.cx_g_codex_todo"),
    acts: [{ label: t("web.setup.app_codex_install"), run: "codex", primary: true }, CLAUDE_INSTEAD()] };
}

function codexLoginBand(xl) {
  const j = xl.job || {};
  if (xl.state === "running" && !j.code) return { kind: "codex_login", level: "hold", text: t("web.setup.app_codex_starting") };
  if (xl.state === "running") {
    const acts = [{ label: t("web.setup.copy"), copy: j.code }];
    if (/^https:\/\//.test(j.url || "")) acts.unshift({ label: t("web.setup.app_codex_open"), href: j.url, primary: true });
    return { kind: "codex_login", level: "hold", text: t("web.setup.app_codex_code"), device: j.code, acts };
  }
  const login = { label: t(xl.state === "failed" ? "web.setup.app_codex_new_code" : "web.setup.app_codex_go"),
    post: ["/api/codex/login", {}], primary: true };
  // the device code lives ~15 min: ran out or did not pass — a new one, the reason under the line
  return { kind: "codex_login", level: xl.state === "failed" ? "fail" : "hold",
    text: t(xl.state === "failed" ? "web.setup.app_codex_expired" : "web.setup.cx_g_login_todo"),
    why: xl.state === "failed" ? reason(j) : "", acts: [login, CLAUDE_INSTEAD()] };
}

function reason(j) {
  return j?.why === "gone" ? t("web.setup.gone") : j?.text || j?.why || "";
}

// the latest update that did not come up and its watchdog went back (not the owner's "Return"), for a day
function notUp(snap) {
  const last = (snap.updates || []).at(-1);
  if (!last || last.ok || last.why !== "rolled-back" || last.by !== "timeout") return null;
  return Date.now() / 1000 - Number(last.ts || 0) < 86400 ? last : null;
}

// a hub from git has no engine switch on the band (it chooses in hub.json) and installs no Codex CLI by itself
function forGit(band) {
  if (!band || appOn()) return band;
  const had = (band.acts || []).some((a) => a.run === "codex");
  const acts = (band.acts || []).filter((a) => a.post?.[0] !== "/api/setup/host" && a.run !== "codex");
  if (had && !acts.length) acts.push({ label: t("web.setup.manual"), href: CODEX_RELEASES, primary: true });
  return { ...band, acts };
}

// the version whose "Restart" was pressed on the band: it says the restart runs until the page reloads (a newer one asks anew)
let restartAsked = null;

export function appBand(status, snap) {
  const s = status, items = Object.fromEntries((s?.items || []).map((i) => [i.id, i]));
  const waits = s?.waits || {}, check = snap.update_check || {};
  const top = limitsBand(snap);
  if (top) return top;
  // 1. what holds the hub: Claude Code, then the sign-in
  const x = items.codex, xl = items.codex_login;
  if (x && x.state !== "ok") return forGit(codexBand(x, waits, !!s.steps_running));
  if (xl && !["ok", "unknown", "wait"].includes(xl.state)) return forGit(codexLoginBand(xl));
  const c = items.claude, l = items.login;
  if (c && c.state !== "ok") return forGit(claudeBand(c, waits, !!s.steps_running));
  if (l && !["ok", "unknown", "wait"].includes(l.state)) return forGit(loginBand(l));
  if (!appOn()) return null;   // a hub from git: the band is for what holds the hub; the rest is in "About the hub"
  // 2. a failure: the update's, the agents' browser, the hub check
  if (check.layer && ["broken", "bad-archive"].includes(check.result))
    return { kind: "update_failed", level: "fail", text: t("web.setup.app_update_broken", { version: check.version || "?" }),
      why: check.error || "", acts: [{ label: t("web.setup.again"), update: true, primary: true }] };
  const back = notUp(snap);
  if (back) return { kind: "update_not_up", level: "fail", text: t("web.setup.app_update_not_up",
    { version: back.version || "?", have: check.have_version || "?" }) };
  const ch = items.chrome;
  if (ch && waits.chrome) return { kind: "chrome_net", level: "fail", text: t("web.setup.app_chrome_net") };
  // "Later" puts off this failure, not every one to come: a new try that fails again is said again
  const libs = `chrome_libs:${ch?.job?.started || ch?.job?.ts || reason(ch?.job)}`;
  if (ch && ch.state === "failed" && ch.system && !hidden(libs)) return { kind: "chrome_libs", level: "fail",
    text: t("web.setup.app_chrome_libs"), why: reason(ch.job), cmd: ch.system,
    acts: [{ label: t("web.setup.copy"), copy: ch.system, primary: true }, { label: t("web.setup.again"), run: "chrome" },
      { label: t("web.setup.later"), hide: libs }] };
  if (ch && ch.state === "failed" && !ch.system) return { kind: "chrome_failed", level: "fail", text: t("web.setup.app_chrome_failed"),
    why: reason(ch.job), acts: [{ label: t("web.setup.again"), run: "chrome", primary: true }] };
  const d = items.doctor, bad = (d?.rows || []).find((r) => r[0] === "fail");
  if (d && d.state === "fail" && !hiddenCheck(d)) return { kind: "doctor", level: "fail",
    text: bad ? t("web.setup.app_doctor_bad", { what: bad[1] }) : t("web.setup.app_doctor_failed"), why: bad?.[2] || reason(d.job),
    acts: [{ label: t("web.setup.again"), run: "doctor", primary: true }, { label: t("web.setup.app_more"), about: true }] };
  // 3. progress: the layer's download, the restart it waits for, the agents' browser
  const u = s?.update;
  if (u) {
    const p = [u.done || 0, u.total || 0];
    if (u.phase === "check") return { kind: "update", level: "progress", text: t("web.setup.app_update_check", { version: u.version }) };
    return { kind: "update", level: "progress", progress: p, text: t(u.first ? "web.setup.app_layer_first" : "web.setup.app_update_download",
      { version: u.version, done: MB(p[0]), total: MB(p[1]) }) };
  }
  if (check.layer && check.result === "restarting" && check.ask_restart && restartAsked !== check.version) {
    // installed in the background, no restart queued (modules/update/layer.py _restart_or_ask): the hub waits for the person's
    // press — the page's own restart, as on the Updates screen, not a "Restarting…" that nothing is doing
    return { kind: "restart_ask", level: "hold", text: t("web.setup.app_restart_ready", { version: check.version || "?" }),
      acts: [{ label: t("web.updates.restart_now"), post: ["/api/restart", { parts: [] }], primary: true,
        done: (r) => { restartAsked = r?.error ? null : check.version; } }] };
  }
  if (check.layer && check.result === "restarting") {
    const busy = (snap.sessions || []).filter((x) => BUSY.includes(x.state)).map((x) => x.name);
    return { kind: "restart", level: "progress", text: busy.length
      ? t("web.setup.app_restart_wait", { version: check.version || "?", count: busy.length, names: busy.slice(0, 5).join(", ") })
      : t("web.setup.app_restart", { version: check.version || "?" }) };
  }
  if (ch && ch.state === "running") {
    const p = ch.job?.progress;
    return { kind: "chrome", level: "progress", progress: p, text: Array.isArray(p) && p[1]
      ? t("web.setup.app_chrome_progress", { done: MB(p[0]), total: MB(p[1]) }) : t("web.setup.chrome_running") };
  }
  // 4. optional: the agents' browser needs one root command (Ubuntu 23.10+: Google Chrome for the whole machine)
  if (ch && ch.state === "todo" && ch.system && !hidden("chrome_root")) return { kind: "chrome_root", level: "hold",
    text: t("web.setup.app_chrome_root"), cmd: ch.system, acts: [{ label: t("web.setup.copy"), copy: ch.system, primary: true },
      { label: t("web.setup.later"), hide: "chrome_root" }] };
  return null;
}

// a band the person put off ("Later") stays away in this browser
const HIDE = "hub.setup.hide";
function hidden(kind) {
  try { return (JSON.parse(localStorage.getItem(HIDE) || "[]") || []).includes(kind); } catch { return false; }
}
function hide(kind) {
  try {
    const had = JSON.parse(localStorage.getItem(HIDE) || "[]");
    const kept = (Array.isArray(had) ? had : []).filter((k) => k.split(":")[0] !== kind.split(":")[0]);
    localStorage.setItem(HIDE, JSON.stringify([...kept, kind]));
  } catch { /* a private window: it comes back next time */ }
}

let bandPainted = "";

async function runOpening(step) {
  // "Sign in to Claude" when no sign-in waits: the window opens on the press (a browser lets a window open only
  // then), the sign-in's link goes into it once the step has it
  const w = window.open("", "_blank");
  if (w) w.opener = null;   // the sign-in's site gets no hold of this page
  const was = item("login")?.job?.started;   // an earlier sign-in's file keeps its expired link: only the new one's
  await run(step);
  const end = Date.now() + 60000;
  const look = () => {
    const j = item("login")?.job || {};
    const fresh = j.state === "running" && j.started !== was;
    const url = fresh ? loginUrl(j) : "";
    if (url && /^https?:\/\//.test(url)) { if (w && !w.closed) w.location.href = url; return; }
    if (Date.now() < end && !(w && w.closed)) setTimeout(look, 500);
    else if (w && !w.closed) w.close();
  };
  look();
}

function act(a) {
  if (a.href) {
    const x = el("a", a.primary ? "btn small primary" : "btn small quiet", a.label);
    x.href = a.href;
    x.target = "_blank";
    x.rel = "noopener noreferrer";
    return x;
  }
  const b = el("button", a.primary ? "btn small primary" : "btn small quiet", a.label);
  b.type = "button";
  b.disabled = !!(a.run && mine[a.run]?.busy) || !!(a.post && mine.band?.busy);
  b.addEventListener("click", () => {
    if (a.run) return a.open ? runOpening(a.run) : run(a.run);
    if (a.about) return dispatchEvent(new CustomEvent("hub-open-settings", { detail: "about", cancelable: true }));
    if (a.post) {
      const kind = setupBar.dataset.kind;
      mine.band = { busy: true, kind };
      b.disabled = true;
      return post(a.post[0], a.post[1]).then((r) => {
        try { a.done?.(r); } catch { /* the band is repainted below either way */ }
        mine.band = { kind, ...(r.error ? { error: r.error } : {}) };
        load();
      });
    }
    if (a.copy) {   // over plain http (a phone) navigator.clipboard is not there: copyText falls back by hand
      return copyText(a.copy).then(() => { b.textContent = t("web.setup.copied"); },
        () => { b.textContent = t("web.setup.copy_failed"); });
    }
    if (a.hide) {
      hide(a.hide);
      bandPainted = "";
      return paintBand();
    }
    if (a.update) {
      dispatchEvent(new Event("hub-update-pressed"));
      return post("/api/update/install", {}).then(() => load());
    }
  });
  return b;
}

function paintBand() {
  const band = st ? appBand(st, S.snap || {}) : null;
  setupBar.hidden = !band;
  if (!band) { bandPainted = ""; return; }
  if (setupBar.parentElement) setupBar.parentElement.hidden = false;
  // this tab's own press: its step's button waits, a refusal of the server is said in the band
  const steps = (band.acts || []).map((a) => a.run).filter(Boolean).concat(band.code ? ["login"] : []);
  // a refusal is said under the band it came from, not under the next one; a device code that came after it wins
  const own = steps.map((k) => mine[k]).concat(mine.band?.kind === band.kind ? [mine.band] : []).find((m) => m?.error);
  if (own && !band.device) band.why = own.error;
  const model = JSON.stringify([band, steps.map((k) => mine[k] || null), mine.band || null, drafts.login ? 1 : 0]);
  if (model === bandPainted && setupBar.childNodes.length) return;   // a field keeps its focus: no redraw for nothing
  bandPainted = model;
  setupBar.dataset.kind = band.kind;
  setupBar.dataset.level = band.level;
  const row = el("div", "setupbar-row");
  const text = el("div", "setupbar-t");
  const tx = el("span", "setupbar-line", band.text);
  tx.setAttribute("role", "status");
  text.append(tx);
  if (band.device) text.append(el("code", "su-device-code setupbar-device", band.device));
  if (band.cmd) text.append(el("code", "setupbar-cmd", band.cmd));
  if (band.why) text.append(el("span", "setupbar-why", band.why));
  if (Array.isArray(band.progress) && band.progress[1]) {
    const bar = progress({ progress: band.progress });
    bar.querySelector(".su-pct")?.remove();   // the line already says "4 of 11 MB"
    text.append(bar);
  }
  row.append(text);
  if (band.code) {
    const f = el("form", "su-code setupbar-code");
    const inp = el("input");
    inp.type = "text";
    inp.name = "code";
    inp.autocomplete = "off";
    inp.spellcheck = false;
    inp.placeholder = t("web.setup.login_code");
    inp.setAttribute("aria-label", t("web.setup.login_code"));
    inp.value = drafts.login || "";
    inp.addEventListener("input", () => { drafts.login = inp.value; });
    const go = el("button", "btn small", t("web.setup.send"));
    go.disabled = !!mine.login?.busy;
    f.addEventListener("submit", (e) => { e.preventDefault(); if (inp.value.trim()) give("login", inp.value.trim()); });
    f.append(inp, go);
    row.append(f);
  }
  const acts = (band.acts || []).map(act);
  if (acts.length) {
    const box = el("div", "setupbar-acts");
    box.append(...acts);
    row.append(box);
  }
  const f = setupBar.contains(document.activeElement) ? document.activeElement : null;
  const name = f?.name, pos = f?.selectionStart;
  setupBar.replaceChildren(row);
  const again = name && setupBar.querySelector(`[name="${name}"]`);
  if (again) {
    again.focus({ preventScroll: true });
    if (pos != null && again.setSelectionRange) again.setSelectionRange(pos, pos);
  }
}

// ---- the cards of "About the hub" -------------------------------------------------------------------------------
function link(text, href, cls = "btn small") {
  const a = el("a", cls, text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

function button(text, onClick, cls = "btn small") {
  const b = el("button", cls, text);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function progress(job) {
  const p = job?.progress;
  if (!Array.isArray(p) || !p[1]) return null;
  const pct = Math.min(100, Math.floor((p[0] * 100) / p[1]));
  const track = el("span", "su-bar");
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  track.setAttribute("aria-valuenow", String(pct));
  const fill = el("span");
  fill.style.width = `${pct}%`;
  track.append(fill);
  const wrap = el("div", "su-prog");
  wrap.append(track, el("span", "su-pct", t("web.setup.mb", { done: (p[0] / 1e6).toFixed(0), total: (p[1] / 1e6).toFixed(0), pct })));
  return wrap;
}

function failure(job) {
  const why = job?.why === "gone" ? t("web.setup.gone") : job?.text || job?.why || "";
  return why ? el("p", "su-err", why) : null;
}

function row(id, title, state, body) {
  const box = el("section", "su-item");
  box.dataset.id = id;
  box.dataset.state = state;
  const head = el("div", "su-head");
  const dot = el("span", "su-dot");
  dot.setAttribute("aria-hidden", "true");
  const label = { ok: t("web.setup.st_ok"), running: t("web.setup.st_running"), failed: t("web.setup.st_failed"),
    fail: t("web.setup.st_failed"), warn: t("web.setup.st_warn"), wait: t("web.setup.st_wait"),
    unknown: t("web.setup.st_unknown"), opt: t("web.setup.st_opt"), unchecked: t("web.setup.st_unchecked") }[state]
    || t("web.setup.st_todo");
  head.append(dot, el("h3", "su-title", title), el("span", "su-state", label));
  box.append(head, ...body.filter(Boolean));
  return box;
}

const p = (text, cls = "su-text") => el("p", cls, text);

// the root command that puts Google Chrome on the machine (modules/setup/chrome.system_command), with its copy button
function systemCmd(cmd) {
  const box = el("div", "su-device su-cmd");
  const copy = button(t("web.setup.copy"), () => copyText(cmd).then(() => { copy.textContent = t("web.setup.copied"); },
    () => { copy.textContent = t("web.setup.copy_failed"); }));
  box.append(el("code", "su-path", cmd), copy);
  return box;
}

function chromeRow(it) {
  const j = it.job || {};
  const body = [];
  if (it.state === "ok") body.push(p(t("web.setup.chrome_ok"), "su-text"), el("code", "su-path", it.path || ""));
  else if (it.state === "running") body.push(p(j.text || t("web.setup.chrome_running")), progress(j));
  else if (it.sandbox !== undefined) {   // Ubuntu 23.10+: Chrome in the user's folder cannot start; a package has a command
    if (it.sandbox) body.push(p(t("web.setup.chrome_sandbox")), el("code", "su-path", it.sandbox));
    else if (it.system) body.push(p(t("web.setup.chrome_system")), systemCmd(it.system));
    else {
      body.push(p(t("web.setup.chrome_sandbox_other")));
      const acts = el("div", "su-acts");
      acts.append(link(t("web.setup.chrome_get"), CHROME));
      body.push(acts);
    }
  } else if (!it.available) {
    body.push(p(t("web.setup.chrome_none")));
    const acts = el("div", "su-acts");
    acts.append(link(t("web.setup.chrome_get"), CHROME));
    body.push(acts);
  } else {
    body.push(p(t("web.setup.chrome_todo", { mb: it.mb })));
    if (it.state === "failed") body.push(failure(j));
    if (it.system) body.push(p(t("web.setup.chrome_libs")), systemCmd(it.system));
    const acts = el("div", "su-acts");
    acts.append(button(t(it.state === "failed" ? "web.setup.again" : "web.setup.chrome_install"), () => run("chrome")));
    body.push(acts);
  }
  return row("chrome", t("web.setup.chrome"), it.state === "todo" ? "opt" : it.state, body);
}

function teamRow(it) {
  const j = it.job || {};
  const body = [p(t("web.setup.team_why"), "su-text su-hint")];
  if (it.state === "ok") body.push(p(it.login ? t("web.setup.team_ok_login", { login: it.login }) : t("web.setup.team_ok")));
  else if (it.state === "running" && j.access === "no-access") {
    body.push(p(t("web.setup.team_invite")));
    const acts = el("div", "su-acts");
    acts.append(link(t("web.setup.team_invite_open"), j.invite || it.invite, "btn small primary"), button(t("web.setup.cancel"), () => cancel(j.step || "github"), "btn small quiet"));
    body.push(acts, p(t("web.setup.team_checking"), "su-text su-wait"));
  } else if (it.state === "running" && j.code) {
    body.push(p(t("web.setup.team_code_how")));
    const code = el("div", "su-device");
    const c = el("code", "su-device-code", j.code);
    const copy = button(t("web.setup.copy"), async () => {
      try { await navigator.clipboard.writeText(j.code); copy.textContent = t("web.setup.copied"); }
      catch { copy.textContent = t("web.setup.copy_failed"); }
    });
    code.append(c, copy);
    const acts = el("div", "su-acts");
    acts.append(link(t("web.setup.team_open"), j.verify || "https://github.com/login/device", "btn small primary"),
      button(t("web.setup.cancel"), () => cancel("github"), "btn small quiet"));
    body.push(code, acts);
  } else if (it.state === "running") {
    body.push(p(j.text || t("web.setup.team_starting"), "su-text su-wait"));
  } else {
    if (!it.gh) body.push(p(t("web.setup.team_no_gh")));
    else body.push(p(it.signed ? t("web.setup.team_signed", { login: it.login || "" }) : t("web.setup.team_todo")));
    if (it.state === "failed") body.push(j.access === "no-access" || j.why === "invite" ? p(t("web.setup.team_not_invited"), "su-err") : failure(j));
    const acts = el("div", "su-acts");
    if (it.gh) acts.append(button(t(it.signed ? "web.setup.team_check" : "web.setup.team_connect"), () => run(it.signed ? "access" : "github"), "btn small primary"));
    if (it.signed || j.access === "no-access") acts.append(link(t("web.setup.team_invite_open"), it.invite, "btn small quiet"));
    body.push(acts);
  }
  return row("team", t("web.setup.team"), it.state === "todo" ? "opt" : it.state, body);
}

function doctorRow(it) {
  const body = [];
  const rows = it.rows || [];
  const bad = rows.filter((r) => r[0] !== "ok");
  if (it.state === "running") body.push(p(t("web.setup.doctor_running"), "su-text su-wait"));
  else if (!rows.length && it.state === "todo") body.push(p(t("web.setup.doctor_todo")));
  else {
    body.push(p(bad.length ? t("web.setup.doctor_bad", { n: bad.length, all: rows.length }) : t("web.setup.doctor_ok", { all: rows.length })));
    if (bad.length) {
      const list = el("ul", "su-doc");
      for (const [level, what, detail] of bad.slice(0, 20)) {
        const li = el("li");
        li.dataset.level = level;
        li.append(el("b", null, what), document.createTextNode(detail ? ` — ${detail}` : ""));
        list.append(li);
      }
      body.push(list);
    }
    if (it.state === "failed" && !rows.length) body.push(failure(it.job));
  }
  const acts = el("div", "su-acts");
  if (it.state !== "running") acts.append(button(t("web.setup.doctor_again"), () => run("doctor")));
  const file = button(t("web.setup.bundle"), () => bundle(file), "btn small quiet");
  file.title = t("web.setup.bundle_hint");
  acts.append(file);
  body.push(acts);
  return row("doctor", t("web.setup.doctor"), it.state === "todo" ? "unchecked" : it.state, body);
}

function paint() {
  paintBand();
  paintAbout();
}

// Settings → About the hub: a card for each thing that is not done — the agents' browser that is not there, the team's
// modules that are not connected, a hub check with remarks (with «Check again» and «A file for support»). All done —
// nothing. What holds the hub (Claude Code, the sign-in) is in the band, on every page
function paintAbout() {
  if (aboutNode?.isConnected) fillAbout();
}

// the block is filled before it is inserted too: an empty one grows above the place the person reads after About is
// rebuilt, and the browser's scroll anchoring moves the page down by its height, on every rebuild
function fillAbout() {
  const chrome = item("chrome"), team = item("team"), doctor = item("doctor");
  const model = JSON.stringify([chrome, team, doctor, mine]);
  if (model === aboutPainted) return;
  aboutPainted = model;
  const out = [];
  if (chrome && chrome.state !== "ok") out.push(chromeRow(chrome));
  if (team && team.visible && team.state !== "ok") out.push(teamRow(team));
  // «Check again» on the card keeps it through the run (it would vanish and the page jump), and a refusal is said
  doctorShown = !!doctor && (["warn", "fail"].includes(doctor.state) || (doctor.state === "running" && doctorShown));
  if (doctorShown) out.push(doctorRow(doctor));
  const err = ["chrome", "doctor", "github", "access"].map((k) => mine[k]).find((m) => m?.error);
  if (out.length && err) out.push(p(err.error, "su-err"));
  aboutNode.replaceChildren(...out);
}

export function appSetupSection() {
  aboutNode = el("div", "su app-setup");
  aboutPainted = "";
  doctorShown = false;
  fillAbout();
  if (!stFull) setTimeout(load);   // the full list now, not at the next slow round — once the block is in the page (full asks isConnected)
  return aboutNode;
}
