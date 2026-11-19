// Settings → Modules → Voice: a section inside the card. The hub's voice is a recognition command in settings
// (voice.cmd) or the built-in model (modules/voice): the models for the hub's language are rows, the recommended one
// first (spec d2126783); a press on a row downloads that model and turns voice on with it — with voice on, a switch:
// the model in use works until the new one passes its check, then it is removed. «Turn on voice» takes the recommended
// one. After that the page has a microphone and Telegram voice messages are transcribed on this machine.
// GET /api/voice/local tells which; POST {on: true, model?} turns it on or switches (the setup step `voice`),
// {cancel: true} stops a switch, {on: false} turns it off. Below — dictation into any program with Handy (the hub does
// not install it). In the team flavor the last row is the cloud one (modules/voice/chatgpt.py, spec d2140189): its facts
// come from the server in the page's words; without the hub's Codex sign-in a press signs in (the link and the code in
// the row), then turns it on.
import { el } from "./dom.js";
import { post } from "./api.js";
import { S } from "./state.js";
import { t, locale } from "./i18n.js";

const HANDY = "https://github.com/danyiimp/handy-codex";
const POLL = 1000;   // ms while the download runs
const MB = (n) => `${Math.round((n || 0) / 1e6)}`;
const CHARS = new Set(["zh", "ja", "ko"]);   // their errors are counted in characters, not words
const ROLE = { recommended: "web.voicehelp.role_recommended", lighter: "web.voicehelp.role_lighter",
  faster: "web.voicehelp.role_faster", accurate: "web.voicehelp.role_accurate", more: "web.voicehelp.role_more" };
let local = null, poller = 0;
let signing = null;   // the cloud row's sign-in going on: {url, code} — when the row can be chosen, it is turned on

function link(text, href) {
  const a = el("a", null, text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener";
  return a;
}

async function load() {
  const was = local?.on;
  try {
    const r = await fetch("/api/voice/local", { headers: { "X-Hub": "1" } });
    local = r.ok ? await r.json() : null;
  } catch {
    local = null;
  }
  // turned on or off: the card's own state (on/off) comes with the modules list — ask for it again
  if (was !== undefined && local && local.on !== was) window.dispatchEvent(new Event("hub-modules"));
}

function button(text, fn, cls = "btn") {
  const b = el("button", cls, text);
  b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true;
    await fn();
  });
  return b;
}

async function act(sec, body) {
  const was = local?.on;
  const r = await post("/api/voice/local", body);
  if (r.data && typeof r.data === "object" && "on" in r.data) local = r.data;
  else await load();
  if (local && was !== undefined && local.on !== was) window.dispatchEvent(new Event("hub-modules"));
  if (r.error) local = { ...(local || {}), error: r.error };
  fill(sec);
}

function watch(sec, wait = POLL) {
  clearTimeout(poller);
  poller = setTimeout(async () => {
    if (!sec.isConnected) return;
    await load();
    if (!cloudReady(sec)) fill(sec);
  }, wait);
}

const num = (x) => (typeof x === "number" ? x.toLocaleString(locale, { maximumFractionDigits: 1 }) : "?");
const size = (n) => (n >= 1e9 ? t("web.voicehelp.gb", { n: num(Math.round(n / 1e8) / 10) }) : t("web.voicehelp.mb", { n: MB(n) }));

// what a row says under the name: errors, speed, size, languages, license, memory; the cloud row — the server's words
function facts(m) {
  if (m.cloud) return m.note || "";
  const parts = [
    t(CHARS.has(m.lang) ? "web.voicehelp.errors_chars" : "web.voicehelp.errors_words", { n: num(m.error) }),
    t("web.voicehelp.wait30", { n: num(m.wait30) }),
    size(m.size),
    m.langs <= 1 ? t("web.voicehelp.one_language")
      : !m.english ? t("web.voicehelp.more_languages", { count: m.langs - 1 })
        : m.langs > 2 ? t("web.voicehelp.with_english", { count: m.langs - 2 }) : t("web.voicehelp.with_english_only"),
    m.file_license ? t("web.voicehelp.two_licenses", { author: m.license, file: m.file_license }) : m.license,
  ];
  if (m.memory) parts.push(t("web.voicehelp.memory", { n: num(Math.round(m.memory / 1e8) / 10) }));
  return parts.filter(Boolean).join(" · ");
}

function rows(sec) {
  const j = local?.job || {};
  const busy = j.state === "running";
  const list = el("div", "voice-models");
  for (const m of local.models) {
    const active = local.on && !local.missing && m.id === local.model;   // files gone: its row downloads it again
    const coming = busy && (j.model ? j.model === m.id : !local.on && m.role === "recommended");
    const row = el(active || busy ? "div" : "button", "voice-model" + (active ? " on" : "") + (coming ? " coming" : ""));
    if (row.tagName === "BUTTON") {
      row.type = "button";
      row.title = m.cloud ? m.name : t(local.on ? "web.voicehelp.switch_to" : "web.voicehelp.turn_on_with", { name: m.name, mb: MB(m.size) });
      row.addEventListener("click", () => {
        row.disabled = true;
        if (m.cloud && m.act) prepare(sec, m);
        else act(sec, { on: true, model: m.id });
      });
    }
    const who = el("span", "voice-model-who");
    const head = el("span", "voice-model-name", m.name);
    if (ROLE[m.role]) head.append(" ", el("span", "voice-model-role", t(ROLE[m.role])));
    who.append(head, el("span", "voice-model-facts", facts(m)));
    const state = el("span", "voice-model-state");
    if (active) state.append(t("web.voicehelp.in_use"));
    else if (coming) {
      const p = Array.isArray(j.progress) ? j.progress : null;
      state.append(p && p[1] ? t("web.voicehelp.downloading", { done: MB(p[0]), total: MB(p[1]) }) : (j.text || t("web.voicehelp.starting")));
      const bar = el("span", "voice-model-bar");
      const fillBar = el("i");
      fillBar.style.width = p && p[1] ? `${Math.min(100, Math.round((100 * p[0]) / p[1]))}%` : "0%";
      bar.append(fillBar);
      state.append(bar);
    }
    row.append(el("span", "voice-model-mark"), who, state);
    list.append(row);
    if (m.cloud && signing?.code && m.act === "login") list.append(codeBox(signing.url, signing.code));
  }
  return list;
}

function codeBox(url, code) {
  const box = el("div", "cx-code");
  box.append(el("p", null, t("web.codex.open")), link(url, url), el("p", null, t("web.codex.enter_code")),
    el("code", "cx-usercode", code), el("p", "ac-muted", t("web.codex.waiting")));
  return box;
}

// the cloud row cannot be chosen yet: Codex is installed (the setup step) or the hub signs in to it; then the row is
// turned on by itself (cloudReady)
async function prepare(sec, m) {
  if (m.act === "codex") {
    await post("/api/setup/run", { step: "codex" });
    signing = { url: "", code: "" };   // after Codex comes, the press signs in
  } else {
    const r = await post("/api/codex/login", { for: "voice" });
    if (r.data && r.data.code) signing = { url: r.data.url, code: r.data.code };
    else local = { ...(local || {}), error: r.error || "" };
  }
  fill(sec);
  watch(sec, 2000);
}

function cloudReady(sec) {
  const m = (local?.models || []).find((x) => x.cloud);
  if (!signing || !m) return false;
  if (m.act) {   // still waiting for Codex or the sign-in
    if (m.act === "login" && !signing.code) prepare(sec, m);
    else watch(sec, 2000);
    return false;
  }
  signing = null;
  act(sec, { on: true, model: m.id });
  return true;
}

function builtIn(sec) {
  const box = el("div", "voice-local");
  const j = local?.job || {};
  if (local?.cmd) {
    box.append(el("p", "jev-state on", t("web.voicehelp.on")), el("p", "hint", t("web.voicehelp.by_command")));
    return box;
  }
  const why = { lang: "web.voicehelp.no_lang", platform: "web.voicehelp.no_platform", ffmpeg: "web.voicehelp.no_ffmpeg" };
  if (!local?.on && why[local?.why]) {
    box.append(el("p", "jev-state", t("web.voicehelp.off")), el("p", "hint", t(why[local.why])));
    return box;
  }
  const busy = j.state === "running";
  if (local?.on && local.cloud) {   // the cloud row: where the sound goes and what takes over, in the server's words
    box.append(el("p", "jev-state on", local.cloud.on_text || ""), el("p", "hint", local.cloud.here || ""));
    if (local.cloud.last) box.append(el("p", "hint warn", local.cloud.last));
    box.append(button(t(busy ? "web.voicehelp.cancel" : "web.voicehelp.turn_off"),
      () => act(sec, busy ? { cancel: true } : { on: false }), "btn small quiet"));
  } else if (local?.on) {
    box.append(el("p", "jev-state on", t("web.voicehelp.local_on", { model: local.name || local.model || "" })),
      el("p", "hint", t("web.voicehelp.local_here")));
    if (why[local.why]) box.append(el("p", "hint warn", t(why[local.why])));
    if (local.missing) box.append(el("p", "hint warn", t("web.voicehelp.missing", { model: local.name || local.model || "" })));
    if (local.hears === false) box.append(el("p", "hint warn", t("web.voicehelp.not_this_language", { model: local.name || local.model || "" })));
    if (busy) box.append(el("p", "hint", t("web.voicehelp.switching", { model: local.name || local.model || "" })));
    box.append(button(t(busy ? "web.voicehelp.cancel" : "web.voicehelp.turn_off"),
      () => act(sec, busy ? { cancel: true } : { on: false }), "btn small quiet"));
  } else {
    // «Try again» takes the model that failed while it is still a row of the hub's language (the language may have changed)
    const again = j.state === "failed" && j.model && (local?.models || []).some((m) => m.id === j.model);
    box.append(el("p", "jev-state", t("web.voicehelp.off")), el("p", "hint", t("web.voicehelp.local_pick")));
    if (busy) box.append(button(t("web.voicehelp.cancel"), () => act(sec, { on: false }), "btn small quiet"));
    else box.append(button(t(j.state === "failed" ? "web.voicehelp.again" : "web.voicehelp.turn_on_with",
      { name: local?.name || "", mb: MB(local?.size) }), () => act(sec, { on: true, ...(again ? { model: j.model } : {}) }),
    "btn small primary"));
  }
  if ((j.state === "failed" && !busy) || local?.error) box.append(el("p", "hint warn", local?.error || j.text || j.why || ""));
  if (Array.isArray(local?.models) && local.models.length) {
    box.append(rows(sec), el("p", "hint", t("web.voicehelp.numbers")));
  }
  if (busy) watch(sec);
  return box;
}

function fill(sec) {
  const p = el("p", "hint");
  const [before, after = ""] = t("web.voicehelp.handy", { app: "\u0000" }).split("\u0000");   // the link sits in the middle of the phrase
  p.append(before, link("Handy", HANDY), after);
  sec.replaceChildren(local ? builtIn(sec) : el("p", "jev-state" + (S.snap.voice ? " on" : ""),
    t(S.snap.voice ? "web.voicehelp.on" : "web.voicehelp.off")), p);
}

export function voiceSection() {
  const sec = el("div", "settings-sec");
  fill(sec);
  load().then(() => {
    if (!sec.isConnected) return;
    fill(sec);
    if (signing) watch(sec, 2000);   // the tab was drawn anew during a sign-in: the old node's wait went with it
  });
  return sec;
}
