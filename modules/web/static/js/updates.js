// Hub updates: it should be visible that an update exists and what was updated. A sidebar item with a "new" mark until
// it has been looked at; the view shows the code version and the update history from the shared repository with
// the CHANGES.md entries; for the shared hub, what is not published yet.
import { el, icon } from "./dom.js";
import { clock } from "./clock.js";
import { S, render } from "./state.js";
import { confirmDlg, toList } from "./dialogs.js";
import { post } from "./api.js";
import { locale, t } from "./i18n.js";

const WHY = { conflict: t("web.updates.why_conflict"), tests: t("web.updates.why_tests"), blocked: t("web.updates.why_blocked") };
const when = (ts) => clock(ts, locale, { day: "numeric", month: "short" });

// the hub is not running the latest version: the new code is already on disk and waits for a restart, or the update
// did not take. New code is checked by tests in a separate copy (the update.testing fact until the check finishes):
// it is not yet "waiting for a restart" — if the tests are red, the hub code will not change (otherwise the page kept
// saying a restart is needed). Stuck for more than 30 min — no longer counted; a hub with a longer check limit
// (update.tests_timeout, Settings) — that limit and 10 min more; a server without the row — 30 min
function testingWindow() {
  const row = ((S.snap || {}).settings || []).find((r) => r.key === "update.tests_timeout");
  const limit = Math.min(Number(row?.value) || 0, Number(row?.max) || 0);
  return Math.max(1800, limit + 600);
}

export function testingVersion() {
  const u = (S.snap || {}).update_testing;
  return u && Date.now() / 1000 - u.ts < testingWindow() ? t("web.updates.testing", { time: when(u.ts) }) : "";
}

export function staleVersion() {
  const snap = S.snap || {};   // the connection opened before the first snapshot
  const code = snap.code || {}, ups = snap.updates || [], last = ups[ups.length - 1];
  if (testingVersion()) return "";
  if (code.head && code.running && code.head !== code.running) return t("web.updates.stale_restart");
  // a failure is cleared if the check after it says "latest version" (a conflict was resolved by hand and the badge hung on)
  const caughtUp = caughtUpSince(last);
  // a return to the previous release (the app) is not a failed install: the code is the one the owner or the watchdog chose
  if (code.upstream && last && !last.ok && !["rolled-back", "cancelled"].includes(last.why) && !caughtUp) return t("web.updates.stale_failed");
  return "";
}

// the check after an update says "latest version": whatever went wrong then, the hub has got past it
function caughtUpSince(last) {
  const check = (S.snap || {}).update_check;
  return check?.result === "current" && check.ts >= last?.ts;
}

// the code version: the release (tag vX.Y.Z) that is already inside, and the commit; the update channel (a hub from
// git: main = nightly; the app's clone: releases)
// a build of the person's edits on a code layer (`hub app apply`): the release it is over + the count
const releaseText = (code) => (code.own?.release ? t("web.updates.own_version", { version: code.own.release, count: code.own.edits ?? "?" })
  : code.version);
const versionText = (code) => [releaseText(code), code.head].filter(Boolean).join(" · ") || "?";
const CHANNEL = { stable: t("web.updates.channel_stable"), main: t("web.updates.channel_main") };
// the pill beside the title: the version alone — the commit is in "About the hub"
const shortVersion = (code) => releaseText(code) || code.head || "?";
function channelText(code) {
  if (!code.upstream) return t("web.updates.updates_self") + (code.release ? ` · ${t("web.updates.last_release_inline", { tag: code.release.tag })}${code.release.at ? " · " + when(code.release.at) : ""}` : "");
  return t("web.updates.updates_every", { channel: CHANNEL[code.update_channel] || CHANNEL.main });
}

// "2026-11-14" → "14 November" in the page language (the year — only when it is not this one)
export function dayText(iso, year = new Date().getFullYear()) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
  if (!m) return iso || "";
  return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString(locale, { day: "numeric", month: "long", ...(+m[1] !== year ? { year: "numeric" } : {}) });
}
// a note's text: `code` becomes <code>, no backticks on screen
function noteText(text) {
  return String(text).split(/`([^`]+)`/).map((part, k) => (k % 2 ? el("code", null, part) : part)).filter((x) => x !== "");
}
// a note is shown whole and wraps: a line cut with an ellipsis did not say that a press opens it
function noteItem(text) {
  const li = el("li");
  li.append(...noteText(String(text).trim()));
  return li;
}
// what the owner opened stays open across redraws of the screen (a snapshot rebuilds it)
const opened = new Map();
// only a press of the owner is kept: "toggle" also fires for the default set here, and a default kept as a choice would
// hold a row open after its reason to be open has gone
function keepOpen(d, key, byDefault) {
  d.open = opened.has(key) ? opened.get(key) : byDefault;
  d.querySelector("summary").addEventListener("click", () => opened.set(key, !d.open));
}
// a release (or a past update) is a line with its date; the notes are inside, the newest release is open. The fold sign
// and the outcome of a past update (state: "ok" | "fail") are icons of the page's set, not Unicode signs; the reason of
// the latest failure stands out (now), one long past reads as the rest
function notesBox(head, notes, key, open, { why = "", now = false, state = "", cls = "" } = {}) {
  // nothing inside (a past update with no notes and no reason) — a plain line: a fold would open an empty row
  const folds = notes.length > 0 || !!why;
  const box = el(folds ? "details" : "div", `upd${cls ? " " + cls : ""}`), sum = el(folds ? "summary" : "div", "upd-head");
  if (folds) {
    const fold = icon("chevron");
    fold.classList.add("upd-fold");
    sum.append(fold);
  }
  if (state) {
    const mark = el("span", "upd-state");
    mark.dataset.state = state;
    mark.append(icon(state === "ok" ? "allow" : "deny"));
    sum.append(mark);
  }
  sum.append(el("span", "upd-title", head));
  box.append(sum);
  if (!folds) return box;
  if (why) box.append(el("div", now ? "why now" : "why", why));
  const ul = el("ul");
  for (const n of notes) ul.append(/^\d{4}-\d{2}-\d{2}$/.test(n) ? el("li", "upd-date", dayText(n)) : noteItem(n));
  box.append(ul);
  keepOpen(box, key, open);
  return box;
}
function changesList(changes) {
  return changes.map((c, k) => notesBox(dayText(c.date), c.items, `c/${c.date}`, k === 0));
}
// "About the hub" lists what is new in full, every day with its notes (no folds: the folded list is the "Updates" screen)
function changesPlain(changes) {
  return changes.map((c) => {
    const box = el("div", "upd"), ul = el("ul");
    box.append(el("div", "upd-head", c.date));
    for (const n of c.items) ul.append(el("li", null, n));
    box.append(ul);
    return box;
  });
}

// "Settings" → "About the hub": what is new is not hidden away; the section carries all the information about the
// version, uptime, what is new, etc.
let about = null, aboutAt = 0, aboutFor = "", aboutAsk = 0;
// The snapshot carries a light code card and light updates (no notes, no CHANGES.md entries, an empty unpublished list —
// the first frame of the stream stays small); the rest comes with /api/about, once per half minute and at once when the
// snapshot says it changed (a new head, another unpublished count, a new update)
const aboutKey = () => {
  const c = S.snap.code || {}, u = (S.snap.updates || []).at(-1);
  return JSON.stringify([c.head, c.running, c.unpublished_count, u?.seq ?? u?.ts]);
};
function loadAbout() {
  const key = aboutKey();
  if (key !== aboutFor || Date.now() - aboutAt > 30000) {
    aboutAt = Date.now();
    aboutFor = key;
    const ask = ++aboutAsk;   // only the latest request counts: an earlier answer arriving late is older data
    fetch("/api/about", { cache: "no-store" }).then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((j) => { if (ask === aboutAsk) { about = j; window.dispatchEvent(new Event("hub-about")); } })
      .catch(() => { if (ask === aboutAsk) aboutAt = Date.now() - 25000; });   // failed (a busy 503): ask again in 5 s, not on every redraw
  }
}
// the live snapshot's code card; only what it leaves out comes from /api/about: the CHANGES.md entries and the unpublished
// list with its count, from one source (an old server still sends all of it in the snapshot)
const fullCode = () => {
  const c = { ...(S.snap.code || {}) };
  if (about?.code && !("changes" in c)) {
    c.changes = about.code.changes;
    if (Array.isArray(c.unpublished)) {
      c.unpublished = about.code.unpublished || [];
      if ("unpublished_count" in about.code) c.unpublished_count = about.code.unpublished_count;
    }
  }
  return c;
};
// the update history of the live snapshot, the notes of each from /api/about by seq (an old server still sends them)
const fullUpdates = () => {
  const notes = new Map((about?.updates || []).filter((u) => u.seq != null).map((u) => [u.seq, u.notes]));
  return (S.snap.updates || []).map((u) => (u.notes || u.seq == null ? u : { ...u, notes: notes.get(u.seq) }));
};
window.addEventListener("hub-about", () => { if (S.selected === ":updates") render(); });
const fmtSize = (b) => b > 1e9 ? t("web.updates.size_gb", { n: (b / 1e9).toFixed(1) }) : b > 1e6 ? t("web.updates.size_mb", { n: (b / 1e6).toFixed(1) }) : t("web.updates.size_kb", { n: Math.round(b / 1e3) });
function uptime(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  const span = `${d ? t("web.updates.unit_d", { n: d }) + " " : ""}${h ? t("web.updates.unit_h", { n: h }) + " " : ""}${t("web.updates.unit_min", { n: m })}`;
  return t("web.updates.uptime", { span, when: when(ts) });
}

// "Check for updates": the outcome of the last check (the update.checked fact) — what came out and what next.
// The check only looks whether a new version exists (it does not verify code); "Install" is a request to the hub (the
// page just passes it on and writes "sent to the hub"), which runs `hub update`: merge, tests (minutes — the
// "tests running" line), restart
// what to do when git could not reach GitHub, by the kind of the failure the core recognised (core/gitaccess.py);
// an unknown kind (an older core) gives nothing
const GIT_HINTS = {
  auth: () => t("web.updates.git_hint_auth"),
  ssh: () => t("web.updates.git_hint_ssh"),
  missing: () => t("web.updates.git_hint_missing"),
  network: () => t("web.updates.git_hint_network"),
};
const gitHint = (kind) => (Object.hasOwn(GIT_HINTS, kind) ? GIT_HINTS[kind]() : "");

const CHECK = {
  current: () => t("web.updates.check_current"),
  // the app (installer spec S3) never installs by itself — its "Update" button does
  // the app on a code layer (modules/update/layer.py): a version, no count of changes
  available: (c) => (c.layer && c.version ? t("web.updates.check_available_layer", { version: c.version })
    : c.app && c.version ? t("web.updates.check_available_app", { version: c.version, behind: c.behind })
    : c.version ? t("web.updates.check_available_version", { version: c.version, behind: c.behind })
    : t("web.updates.check_available", { behind: c.behind })),
  // a layer installs in the background and asks the person for the restart (modules/update/layer.py _restart_or_ask)
  applied: (c) => (c.ask_restart && c.version ? t("web.updates.check_ready_layer", { version: c.version })
    : t("web.updates.check_applied")),
  dirty: (c) => t("web.updates.check_dirty", { behind: c.behind }),
  conflict: (c) => t("web.updates.check_conflict", { behind: c.behind }),
  red: (c) => t("web.updates.check_red", { behind: c.behind }),
  busy: (c) => t("web.updates.check_busy", { behind: c.behind }),
  // the Mac is between sleeps (a maintenance wake) or slept during the tests: no result, the next pass checks again
  asleep: (c) => t("web.updates.check_asleep", { behind: c.behind }),
  // the check runs in a separate copy: the hub code changed while the tests ran — again in a few minutes;
  // a non-git file blocks the fast-forward; the app's clone without a release tag; the new code does not continue the installed
  // one; the tag was moved
  moved: (c) => t("web.updates.check_moved", { behind: c.behind }),
  blocked: (c) => t("web.updates.check_blocked", { behind: c.behind }),
  "no-release": () => t("web.updates.check_no_release"),
  "not-descendant": () => t("web.updates.check_not_descendant"),
  "tag-moved": (c) => t("web.updates.check_tag_moved", { version: c.version || "?" }),
  "no-remote": () => t("web.updates.check_no_remote"),
  disabled: () => t("web.updates.check_disabled"),
  // off and the code has no origin (a clone made without --origin): the cause and the way out, not "off in the settings"
  "no-origin": () => t("web.updates.check_no_origin"),
  // a second hub on the same code folder (update.follow): the first hub merges the code
  following: (c) => (c.behind
    ? (c.version ? t("web.updates.check_following_behind_version", { version: c.version, behind: c.behind })
      : t("web.updates.check_following_behind", { behind: c.behind }))
    : (c.have_version ? t("web.updates.check_following_current_version", { version: c.have_version })
      : t("web.updates.check_following_current"))),
  error: (c) => t("web.updates.check_error", { error: c.error || t("web.updates.no_connection") }) + (gitHint(c.hint) ? " — " + gitHint(c.hint) : ""),
  // the new version needs a newer Claude Code than the installed one: it is not installed and waits
  "needs-claude": (c) => t("web.updates.check_needs_claude", { behind: c.behind, need: c.need_claude, have: c.have_claude }),
  // the new release waits for its own Claude Code (core/ownclaude): the download failed, the hub tries again
  "claude-wait": (c) => t("web.updates.check_claude_wait", { need: c.need_claude }),
  // a new version without the built-in code of a feature waits until a module provides the feature (module extraction spec)
  "needs-module": (c) => t("web.updates.check_needs_module", { behind: c.behind, list: (c.need_module || []).join("; ") || "?" }),
  // the app: the releases are on GitHub and the hub is not signed in there; a release needs a newer app environment
  "no-access": () => t("web.updates.check_no_access"),
  "rolled-back": (c) => t("web.updates.check_rolled_back", { version: c.version || "?" }),
  // the new app is on the distribution server (app spec §4.4): "Update" puts it in place (Mac, Windows); Linux — the .deb
  "needs-runtime": (c) => (c.shell?.auto
    ? t(c.shell.deb ? "web.updates.check_needs_shell_deb" : "web.updates.check_needs_shell",
      { version: c.version || "?", need: c.need_runtime ?? "?", have: c.have_runtime ?? "?" })
    : t("web.updates.check_needs_runtime", { behind: c.behind, need: c.need_runtime ?? "?", have: c.have_runtime ?? "?" })),
  "shell-ready": (c) => t("web.updates.check_shell_ready", { need: c.shell?.runtime ?? c.need_runtime ?? "?" }),
  "shell-downloaded": (c) => t("web.updates.check_shell_downloaded", { file: c.shell?.file || "?" }),
  "bad-shell": (c) => t("web.updates.check_bad_shell", { error: c.error || "?" }),
  // the code layer: the update server did not answer (a state, it looks again); a download broke off or did not pass the
  // check (nothing changed, "Update" tries again); a build without a server; installed, the restart waits for sessions
  offline: () => t("web.updates.check_offline"),
  broken: (c) => t("web.updates.check_broken", { error: c.error || "?" }),
  "bad-archive": (c) => t("web.updates.check_bad_archive", { error: c.error || "?" }),
  "no-source": () => t("web.updates.check_no_source"),
  restarting: (c) => (c.ask_restart ? t("web.updates.check_ready_layer", { version: c.version || "?" })
    : t("web.updates.check_restarting", { version: c.version || "?" })),
};

// the hub's private modules (modules/update/private.py): installed — which ones; not — why (no access to the modules repository...)
function privateText(p) {
  const mods = Object.entries(p.modules || {});
  const shown = (s) => mods.filter(([, v]) => v === s).map(([n]) => n).join(", ");
  if (p.ok) return t("web.updates.private_ok", { list: shown("ready") || "—" }) + (p.declined?.length ? " · " + t("web.updates.private_declined", { list: p.declined.join(", ") }) : "");
  // no access: the cause git named, as a hint for its kind; one the core did not recognise is shown as git worded it
  if (!p.access) {
    const said = String(p.why || "").replace(/\s+/g, " ").trim().slice(0, 200);
    return t("web.updates.private_no_access", { when: when(p.ts), cause: gitHint(p.hint) || (said ? t("web.updates.private_cause_other", { why: said }) : "") });
  }
  return t("web.updates.private_failed", { list: (p.missing || []).join(", ") || "—", why: p.why || "?" });
}
let asked = 0, sentToHub = 0;   // when "Check" was pressed (until a newer result arrives — "Looking..."), when "Install" was
// the check a press or an error belongs to, by the hub's own time (a browser's clock may be off): a newer check ends it
const onCheck = (c) => c?.ts ?? -1;
// an action of the check line that failed: the buttons redraw the screen (hub-about), so the error is kept here and the
// redrawn line says it, until the next press or a newer check
let failed = null;
// the check line itself says the restart and offers the button: not while a check runs or a version is on trial
const saysRestart = (c, waiting, testing) => !waiting && !testing && !!c?.ask_restart
  && ["applied", "restarting", "shell-ready"].includes(c?.result);
const checkWaiting = (c) => !!asked && !(c && c.ts * 1000 >= asked);
let sentRestart = 0;   // when "Restart" of an installed release was pressed: the line says so until the page reloads
// "Ask the hub to update Claude Code" and "Return" were pressed on this check (onCheck): the line says so and the button
// waits for a newer check (or the outcome the update process writes)
let sentClaude = null, sentBack = null;
// a press after an error: the headings that stood for the line go, so the screen is drawn again
const pressed = () => { if (failed) { failed = null; window.dispatchEvent(new Event("hub-about")); } };
// the line says the restart or the version on trial only when it shows no error: else the headings say them
const lineSaysRestart = (c, waiting, testing) => !failed && saysRestart(c, waiting, testing);

// the app's buttons go straight to the update process (POST /api/update/install|rollback); a server from before them
// answers 404 — then, as before, a request to the hub session
async function appAct(path, body, hub, text) {
  const r = await post(path, body);
  if (r.status !== 404) return r;
  return hub ? post("/api/send", { session: hub.name, text, action: true }) : r;
}

// compatibility with Claude Code (the hub shows what is compatible and what is not): the versions of live sessions
// against the oldest one this release works with (core/version.py MIN_CLAUDE)
const semver = (v) => String(v || "").split(".").map((x) => parseInt(x, 10) || 0);
const older = (a, b) => { const x = semver(a), y = semver(b); for (let i = 0; i < 3; i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) < (y[i] || 0); return false; };
export function claudeCompat() {
  // a hub on Codex runs no Claude Code — its sessions' versions are Codex's, MIN_CLAUDE is not theirs
  if (S.snap.codex) return el("div", "claude-compat");
  const need = S.snap.min_claude;
  // only with a live process: a sleeping session keeps the version of its previous launch, but wakes up on the current claude
  const live = (S.snap.sessions || []).filter((s) => ["starting", "idle", "busy", "dialog"].includes(s.state) && s.info?.version);
  const versions = [...new Set(live.map((s) => s.info.version))].sort((a, b) => (older(a, b) ? -1 : 1));
  const box = el("div", "claude-compat");
  const head = t("web.updates.compat_head", { versions: versions.join(", ") || t("web.updates.compat_unknown") })
    + (need ? ` · ${t("web.updates.compat_need", { need })}` : "");
  box.append(el("p", "hint", head));
  const bad = need ? live.filter((s) => older(s.info.version, need)) : [];
  if (bad.length) box.append(el("p", "hint warn", t("web.updates.compat_old", { list: bad.map((s) => `${s.name} (${s.info.version})`).join(", ") })));
  return box;
}

function updateCheck() {
  const c = S.snap.update_check;
  const box = el("div", "upd-check");
  const waiting = checkWaiting(c);
  const testing = testingVersion();
  if (failed && onCheck(c) !== failed.on) failed = null;
  // a button's answer comes after a snapshot may have drawn the screen again: the error is kept and the screen redrawn
  const fail = (error) => {
    failed = { text: t("web.updates.failed", { error }), on: onCheck(S.snap.update_check) };
    line.textContent = failed.text;
    window.dispatchEvent(new Event("hub-about"));
  };
  const line = el("p", "hint", failed ? failed.text : testing ? testing[0].toUpperCase() + testing.slice(1)
    : waiting ? t("web.updates.checking")
    : sentToHub && c?.result === "available" ? t("web.updates.sent_install")
    : sentRestart && c?.ask_restart ? t("web.updates.restart_sent")
    : sentClaude === onCheck(c) && c?.result === "needs-claude" ? t("web.updates.sent_claude")
    : sentBack === onCheck(c) && c?.previous ? t("web.updates.sent_rollback", { version: c.previous })
    : !c ? t("web.updates.not_checked")
    : t("web.updates.status", { result: (CHECK[c.result] || (() => c.result))(c), when: when(c.ts) }));
  const row = el("div", "upd-actions");
  // a layer's download that broke off or did not pass the check: "Update" tries again
  // the new app that "Update" puts in place, or one that did not pass the check (it is downloaded again)
  const available = !waiting && !testing && (c?.result === "available" || (c?.layer && ["broken", "bad-archive", "bad-shell"].includes(c?.result))
    || (c?.result === "needs-runtime" && c.shell?.auto));
  const hub = (S.snap.sessions || []).find((x) => x.kind === "hub");
  if (available && (hub || c.app) && !sentToHub) {
    const b = el("button", "btn small primary", c.app ? t("web.updates.update_app") : t("web.updates.install"));
    b.type = "button";
    b.addEventListener("click", async () => {
      sentToHub = Date.now(); failed = null;
      const text = c.app && c.version ? t("web.updates.msg_update_app", { version: c.version }) : t("web.updates.msg_install", { behind: c.behind });
      const r = c.app ? await appAct("/api/update/install", {}, hub, text) : await post("/api/send", { session: hub.name, text, action: true });
      if (r.error) { sentToHub = 0; fail(r.error); return; }
      if (c.layer) window.dispatchEvent(new Event("hub-update-pressed"));   // the app's band shows the download (setup.js)
      window.dispatchEvent(new Event("hub-about"));
    });
    row.append(b);
  }
  if (saysRestart(c, waiting, testing)) {
    // installed in the background, waiting for the person's restart: the whole hub, when sessions are free (as
    // `hub restart`); the owner's own install has its restart queued already — no button
    const r = el("button", "btn small primary", t("web.updates.restart_now"));
    r.type = "button";
    r.disabled = !!sentRestart;
    r.addEventListener("click", async () => {
      sentRestart = Date.now(); pressed();
      r.disabled = true;
      line.textContent = t("web.updates.restart_sent");
      const got = await post("/api/restart", { parts: [] });
      if (got.error) { sentRestart = 0; r.disabled = false; fail(got.error); }
    });
    row.append(r);
  }
  if (!waiting && !testing && c?.result === "needs-claude" && hub) {
    // the button just writes to the hub: it will update Claude Code and install the new hub version
    const k = el("button", "btn small primary", t("web.updates.ask_claude"));
    k.type = "button";
    k.disabled = sentClaude === onCheck(c);
    k.addEventListener("click", async () => {
      sentClaude = onCheck(c); pressed();
      k.disabled = true;
      line.textContent = t("web.updates.sent_claude");
      const r = await post("/api/send", { session: hub.name, text: t("web.updates.msg_claude", { need: c.need_claude, have: c.have_claude }),
        action: true });
      if (r.error) { sentClaude = null; fail(r.error); }
    });
    row.append(k);
  }
  if (!waiting && !testing && c?.result === "needs-runtime" && !c.shell?.auto && /^https:\/\//.test(c.download || "")) {
    // a newer app is a download, not an update of the code: the link the app shell named (its PACKAGED)
    const a = el("a", "btn small primary", t("web.updates.download_app"));
    a.href = c.download;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    row.append(a);
  }
  if (!waiting && !testing && c?.app && c.previous && c.result !== "rolled-back") {
    // the app: back to the previous release (`hub update rollback` — the hub runs it), the schedule then skips the returned one
    // on a build of the person's edits the previous is the release it is over: "Return the released" takes them off
    const back = el("button", "btn small", t(c.own ? "web.updates.rollback_own" : "web.updates.rollback", { version: c.previous }));
    back.type = "button";
    back.disabled = sentBack === onCheck(c);   // until the update process answers (the outcome or the check after it)
    back.addEventListener("click", async () => {
      sentBack = onCheck(c); pressed();
      back.disabled = true;
      line.textContent = t("web.updates.sent_rollback", { version: c.previous });
      const r = await appAct("/api/update/rollback", { version: c.previous }, hub, t("web.updates.msg_rollback", { version: c.previous }));
      if (r.error) { sentBack = null; back.disabled = false; fail(r.error); }
    });
    row.append(back);
  }
  const b = el("button", "btn small", t("web.updates.check"));
  b.type = "button";
  b.disabled = !!(waiting || testing);
  b.addEventListener("click", async () => {
    asked = Date.now(); sentToHub = 0; failed = null;
    window.dispatchEvent(new Event("hub-about"));
    const r = await post("/api/update/check", {});
    if (r.error) { asked = 0; fail(r.error); }   // the redraw above replaced this line on the Updates screen
  });
  row.append(b);
  box.append(line);
  box.append(row);
  if (!waiting && c && c.notes?.length && ["available", "dirty", "conflict", "red", "busy", "asleep", "needs-claude", "claude-wait", "needs-module", "needs-runtime"].includes(c.result)) {
    const ul = el("ul", "upd-notes");
    for (const n of c.notes) ul.append(el("li", null, n));
    box.append(el("p", "hint", available ? t("web.updates.notes_new") : t("web.updates.notes_that")), ul);
  }
  return box;
}

// the red band about the hub's own edits of the core (modules/update local_edits, Plugin API 1.33): every update of such a hub
// runs the core tests and may meet conflicts. "Fix" is a task for Hub Developer (POST /api/update/fix), not a reset from
// the page. An older core sends no local_edits: no band. It is drawn above every tab of "Settings" (settings.js)
const EDIT_KIND = { M: t("web.updates.edit_changed"), A: t("web.updates.edit_added"), D: t("web.updates.edit_deleted"), "?": t("web.updates.edit_new") };
// edits by what they touch (core/codeedits.ZONES, Plugin API 1.36 local_edits.zones): one plain line per zone, in this order
const ZONE_TEXT = {
  guards: (count) => t("web.updates.zone_guards", { count }),
  access: (count) => t("web.updates.zone_access", { count }),
  data_out: (count) => t("web.updates.zone_data_out", { count }),
  update: (count) => t("web.updates.zone_update", { count }),
  journal: (count) => t("web.updates.zone_journal", { count }),
  prompts: (count) => t("web.updates.zone_prompts", { count }),
  tests: (count) => t("web.updates.zone_tests", { count }),
};
let editsOpen = false, editsSent = 0, editsNote = "";   // the list is open, when "Fix" was pressed, what the last press said
let switchSent = 0;   // when "Take off" or "Put back" was pressed: both wait for the check the update process runs after it

// "Take off" (to: off) and "Put back" (to: on), Plugin API 1.36: the update process does it under the machine lock and
// writes the result to the hub's feed (modules/update/takeoff.py); a confirmation first — the hub restarts after it. An
// older page server does not know the path (404): it needs «hub restart web», as "Fix"
function switchSettled() {   // a check after the press: the result is in the hub's feed, the note goes
  const c = S.snap.update_check || {};
  if (switchSent && (c.ts || 0) * 1000 > switchSent) {
    switchSent = 0;
    editsNote = "";
  }
}

function switchButton(to, line) {
  const c = S.snap.update_check || {}, back = c.local_edits?.backup || {};
  const off = to === "off";
  const b = el("button", "btn small", off ? t("web.updates.local_edits_off") : t("web.updates.local_edits_on"));
  b.type = "button";
  b.dataset.act = to;
  b.disabled = switchSent > 0 && switchSent > (c.ts || 0) * 1000;   // until a check after the press shows what is left
  b.addEventListener("click", () => confirmDlg(
    off ? t("web.updates.local_edits_off_title") : t("web.updates.local_edits_on_title"),
    off ? t("web.updates.local_edits_off_text") : t("web.updates.local_edits_on_text", { branch: back.name || "" }),
    off ? t("web.updates.local_edits_off_ok") : t("web.updates.local_edits_on_ok"), async () => {
      switchSent = Date.now();
      b.disabled = true;
      const r = await post("/api/update/local-edits", { to });
      if (r.error && r.status !== 409) {   // 409: already on its way or nothing left — say it, stay locked
        switchSent = 0;
        b.disabled = false;
      }
      editsNote = !r.error ? t("web.updates.local_edits_asked")
        : r.status === 404 ? t("web.updates.local_edits_switch_old_server") : t("web.updates.local_edits_failed", { error: r.error });
      line.textContent = editsNote;
      if (!line.isConnected) render();   // the tab was rebuilt while the request went: the new line shows the note
    }, off ? "danger" : ""));
  return b;
}

// the last take-off that is not put back yet (local_edits.backup): a calm line with "Put back" in "About the hub" while
// the band is gone (no edits left); with edits again, the band carries the button itself
function backupLine() {
  const e = S.snap.update_check?.local_edits, back = e?.backup;
  if (!back || typeof back.name !== "string" || (Number.isInteger(e.count) && e.count > 0)) return null;
  switchSettled();
  const p = el("div", "hint le-backup");
  const line = el("span", "hint", editsNote);
  p.append(el("span", null, t("web.updates.local_edits_backup", { count: back.count || 0, branch: back.name,
    time: back.at ? when(back.at) : "?" })), " ", switchButton("on", line), " ", line);
  return p;
}
// the app's code layer (core/appwatch.py): an edited layer — a line, its files and "Return the released" (the shell's
// launcher puts the release back, the hub restarts); the release put back because the core did not start on an edited
// layer — one line for a week. A hub from git or an older core — no key, nothing.
let appSent = 0, appNote = "", appFilesOpen = false;
const APP_LINE_DAYS = 7;
function appCodeLine() {
  const a = S.snap.app_code;
  if (!a || typeof a !== "object") return null;
  const box = el("div", "app-code");
  const r = a.restored && typeof a.restored === "object" ? a.restored : null;
  const fresh = r && Number(r.at) > Date.now() / 1000 - APP_LINE_DAYS * 86400;
  if (fresh && r.why === "start" && !r.error) box.append(el("p", "hint warn app-restored", t("web.updates.app_restored_start")));
  const count = Number.isInteger(a.count) ? a.count : 0;
  if (fresh && r.error && count > 0) box.append(el("p", "hint warn app-restore-failed", t("web.updates.app_restore_failed", { error: String(r.error) })));
  if (count > 0) {
    box.append(el("p", "hint warn app-modified", t("web.updates.app_modified", { count })));
    const paths = Array.isArray(a.paths) ? a.paths.filter((p) => typeof p === "string") : [];
    if (paths.length) {
      const d = el("details", "app-files");
      d.open = appFilesOpen;
      d.addEventListener("toggle", () => { appFilesOpen = d.open; });   // the tab is rebuilt with the snapshot
      const ul = el("ul");
      for (const p of paths) {
        const li = el("li");
        li.append(el("code", null, p));
        ul.append(li);
      }
      if (count > paths.length) ul.append(el("li", "le-more", t("web.updates.local_edits_more", { count: count - paths.length })));
      d.append(el("summary", null, t("web.updates.app_modified_files")), ul);
      box.append(d);
    }
    const row = el("div", "upd-actions");
    const b = el("button", "btn small", t("web.updates.app_restore"));
    b.type = "button";
    b.dataset.act = "app-restore";
    // until the core says what came of it: the layer checked again or the launcher's record (a failed restore too)
    b.disabled = appSent > 0 && appSent > Math.max(Number(a.at) || 0, Number(r?.at) || 0) * 1000;
    const line = el("span", "hint", b.disabled ? t("web.updates.app_restoring") : appNote);
    b.addEventListener("click", async () => {
      appSent = Date.now();
      appNote = "";
      b.disabled = true;
      const res = await post("/api/app/restore", {});
      if (res.error) {
        appSent = 0;
        appNote = res.status === 404 ? t("web.updates.local_edits_old_server") : String(res.error);
        b.disabled = false;
      }
      line.textContent = res.error ? appNote : t("web.updates.app_restoring");
    });
    row.append(b, line);
    box.append(row);
  }
  return box.childNodes.length ? box : null;
}

// reports of failed updates (modules/update/report.py, app spec §7.1): an app whose build names where reports go says
// what a report holds, with the switch (hub.json update.report). The check carries on/off; a press is this tab's until the
// next check says it. A hub from git, the open build without an address, an older core — no key, no line.
let reportSet = null;   // {on, at}: the last press, until a check after it
function reportLine() {
  const c = S.snap.update_check, r = c?.layer && c.report && typeof c.report.on === "boolean" ? c.report : null;
  if (!r) return null;
  if (reportSet && c.ts * 1000 >= reportSet.at) reportSet = null;
  const on = reportSet ? reportSet.on : r.on;
  const l = el("label", "opt update-report");
  const box = el("input");
  box.type = "checkbox";
  box.checked = on;
  box.dataset.act = "update-report";
  const hint = el("small", null, on ? t("web.updates.report_line") : t("web.updates.report_off"));
  const note = el("span", "hint set-note");
  box.addEventListener("change", async () => {
    const want = box.checked;
    box.disabled = true;
    const res = await post("/api/settings/set", { key: "update.report", value: want });
    box.disabled = false;
    if (res.error) {
      box.checked = !want;
      note.textContent = String(res.error);
      return;
    }
    reportSet = { on: want, at: Date.now() };
    note.textContent = "";
    hint.textContent = want ? t("web.updates.report_line") : t("web.updates.report_off");
  });
  const txt = el("span");
  txt.append(document.createTextNode(t("web.updates.report_toggle")), hint, note);
  l.append(box, txt);
  return l;
}

// the move from a git clone (core/appmove.py, app spec §8): the clone it came from, the backup branch of its own edits
// and, for 14 days, "Return to the clone" (hub app undo: the service back on the clone, hub.json from its copy). The
// button asks once more: the page goes away with the app's hub
let moveAsk = false, moveSent = false, moveNote = "";
function appMoveLine() {
  const m = S.snap.app_move;
  if (!m || typeof m !== "object" || typeof m.repo !== "string") return null;
  const box = el("div", "app-move");
  box.append(el("p", "hint app-moved", t("web.updates.app_moved", { repo: m.repo, time: m.done ? when(m.done) : "?" })));
  if (typeof m.branch === "string" && m.branch) box.append(el("p", "hint app-moved-branch", t("web.updates.app_moved_branch", { branch: m.branch })));
  const row = el("div", "upd-actions");
  const askText = () => moveSent ? t("web.updates.app_undoing")
    : moveNote || (moveAsk ? t("web.updates.app_undo_what", { time: m.until ? when(m.until) : "?" })
      : t("web.updates.app_undo_until", { time: m.until ? when(m.until) : "?" }));   // a redraw keeps the line the press set
  const line = el("span", "hint", askText());
  const go = el("button", "btn small", t(moveAsk ? "web.updates.app_undo_sure" : "web.updates.app_undo"));
  go.type = "button";
  go.dataset.act = "app-undo";
  go.disabled = moveSent;
  go.addEventListener("click", async () => {
    if (!moveAsk) {
      moveAsk = true;
      go.textContent = t("web.updates.app_undo_sure");
      line.textContent = askText();
      return;
    }
    moveSent = true;
    go.disabled = true;
    const res = await post("/api/app/undo", {});
    if (res.error) {
      moveSent = false;
      moveNote = res.status === 404 ? t("web.updates.local_edits_old_server") : String(res.error);
      go.disabled = false;
    }
    line.textContent = askText();
    if (!line.isConnected) render();   // the page was redrawn while the request went: the new line shows the answer
  });
  row.append(go, " ", line);
  box.append(row);
  return box;
}

export function localEditsBand() {
  const c = S.snap.update_check, e = c?.local_edits;
  if (!e || !Number.isInteger(e.count) || e.count < 1) return null;
  switchSettled();
  const box = el("div", "local-edits");
  box.setAttribute("role", "alert");
  box.append(el("p", "le-head", t("web.updates.local_edits", { count: e.count })));
  const zones = e.zones && typeof e.zones === "object" ? e.zones : {};   // an older core sends none: no lines
  const lines = Object.keys(ZONE_TEXT).filter((k) => Number.isInteger(zones[k]) && zones[k] > 0);
  if (lines.length) {
    const ul = el("ul", "le-zones");
    for (const k of lines) {
      const li = el("li", "le-zone", ZONE_TEXT[k](zones[k]));
      li.dataset.zone = k;
      ul.append(li);
    }
    box.append(ul);
  }
  // what the update found against the release (Plugin API 1.36: local_edits.shadow — the release's tests red on the
  // hub's code; local_edits.lost — the release's changes a merge may have lost, Hub Developer looks); an older core: none
  const red = Array.isArray(e.shadow?.failed) ? e.shadow.failed.filter((n) => typeof n === "string") : [];
  const lost = Array.isArray(e.lost?.items) ? e.lost.items.filter((i) => i && typeof i.path === "string") : [];
  if (red.length || lost.length) {
    const ul = el("ul", "le-checks");
    for (const n of red) ul.append(el("li", "le-shadow", t("web.updates.shadow_red", { test: n })));
    if (Number.isInteger(e.shadow?.count) && e.shadow.count > red.length)
      ul.append(el("li", "le-more", t("web.updates.checks_more", { count: e.shadow.count - red.length })));
    for (const i of lost) {
      const li = el("li", "le-lost", t("web.updates.lost_line", { path: i.path }));
      li.dataset.id = String(i.id || "");
      ul.append(li);
    }
    if (Number.isInteger(e.lost?.count) && e.lost.count > lost.length)
      ul.append(el("li", "le-more", t("web.updates.checks_more", { count: e.lost.count - lost.length })));
    box.append(ul);
  }
  const files = Array.isArray(e.files) ? e.files.filter((f) => f && typeof f.path === "string") : [];
  if (files.length) {
    const d = el("details", "le-files");
    d.open = editsOpen;
    d.addEventListener("toggle", () => { editsOpen = d.open; });   // the band is rebuilt with the snapshot
    const ul = el("ul");
    for (const f of files) {
      const li = el("li");
      li.append(el("span", "le-st", EDIT_KIND[f.st] || String(f.st || "?")), " ", el("code", null, f.path));
      ul.append(li);
    }
    if (e.count > files.length) ul.append(el("li", "le-more", t("web.updates.local_edits_more", { count: e.count - files.length })));
    d.append(el("summary", null, t("web.updates.local_edits_files")), ul);
    box.append(d);
  }
  const row = el("div", "upd-actions");
  const b = el("button", "btn small primary", t("web.updates.local_edits_fix"));
  b.type = "button";
  b.dataset.act = "fix";
  b.disabled = editsSent > 0 && editsSent > (c.ts || 0) * 1000;   // until a check after the task shows what is left
  const line = el("span", "hint", b.disabled ? t("web.updates.local_edits_sent") : editsNote);
  b.addEventListener("click", async () => {
    editsSent = Date.now();
    editsNote = "";
    b.disabled = true;
    const r = await post("/api/update/fix", {});
    if (r.error && r.status === 409) {   // already handed over for this check (another tab, a reload) or no edits any more: say it, stay locked
      editsNote = r.error;
      line.textContent = editsNote;
      return;
    }
    if (r.error) {
      editsSent = 0;
      editsNote = r.status === 404 ? t("web.updates.local_edits_old_server") : t("web.updates.local_edits_failed", { error: r.error });
      b.disabled = false;
    }
    line.textContent = r.error ? editsNote : t("web.updates.local_edits_sent");
  });
  row.append(b, switchButton("off", line), ...(e.backup?.name ? [switchButton("on", line)] : []), line);
  box.append(row);
  return box;
}

let aboutMore = false;
export function aboutSection() {
  loadAbout();   // every half minute and when the snapshot says the code or the updates changed, not on every snapshot
  const code = fullCode(), a = about || {};
  const wrap = el("div", "about");
  const facts = [
    ["version", t("web.updates.fact_version"), `${versionText(code)}${code.date ? " · " + when(Date.parse(code.date) / 1000) : ""}`],
    ["core", t("web.updates.fact_core"), uptime(a.core_started)],
    ["page", t("web.updates.fact_page"), uptime(a.web_started)],
    ["api", t("web.updates.fact_api"), a.plugin_api || S.snap.plugin_api || "1.0"],
    ["sessions", t("web.updates.fact_sessions"), a.sessions ?? (S.snap.sessions || []).length],
    ["journal", t("web.updates.fact_journal"), a.journal != null ? `${fmtSize(a.journal)}${a.archive ? " · " + t("web.updates.archive", { size: fmtSize(a.archive) }) : ""}` : "—"],
    // a hub on a code layer (installed by the line) takes updates from the distribution server, with no private modules
    // repository: neither "this is the shared hub itself" nor the last word of the git-era modules step is true there
    ...(code.layer ? [] : [["updates", t("web.updates.fact_updates"), channelText(code)]]),
    ...(S.snap.private_modules && !code.layer ? [["private", t("web.updates.fact_private"), privateText(S.snap.private_modules)]] : []),
    ["python", "Python", a.python ? `${a.python} · ${a.os || ""}` : "—"],
    ["code", t("web.updates.fact_code"), a.repo || "—"],
    ["state", t("web.updates.fact_state"), a.state || "—"],
  ];
  const dl = el("dl", "about-facts"), dl2 = el("dl", "about-facts"), more = el("details", "set-more");
  // version and updates come first; technical items (paths, Python, API) go under "More"
  // private modules that are not installed stay in view: the person needs to take a step (accept an invitation)
  const first = (id) => ["version", "core", "updates"].includes(id) || (id === "private" && !S.snap.private_modules.ok);
  for (const [id, k, v] of facts) (first(id) ? dl : dl2).append(el("dt", null, k), el("dd", null, String(v)));
  more.open = aboutMore;
  more.addEventListener("toggle", () => { aboutMore = more.open; });   // the tab is rebuilt with the snapshot
  more.append(el("summary", null, t("web.updates.more")), dl2);
  wrap.append(el("h3", "day", t("web.updates.about")), dl, more, updateCheck(), claudeCompat());
  const report = reportLine();
  if (report) wrap.append(report);
  // the hub's own modules in the code folder: a calm line, not an edit of the core (core/codeedits.py); an older core has no list
  const own = S.snap.update_check?.local_edits?.modules;
  if (Array.isArray(own) && own.length) wrap.append(el("p", "hint own-modules", t("web.updates.own_modules", { list: own.join(", ") })));   // the hub name is under "General"
  const back = backupLine();
  if (back) wrap.append(back);
  const layer = appCodeLine();
  if (layer) wrap.append(layer);
  const moved = appMoveLine();
  if (moved) wrap.append(moved);
  // a module with the core.write right wrote to the core state (core/corewrite.py): always visible until the owner dismisses it
  for (const [name, r] of Object.entries(S.snap.core_written || {})) {
    if (r.forgot) continue;
    const args = { name, since: r.since ? when(r.since) : "?", count: r.count || 0 };
    wrap.append(el("p", "hint core-written", r.on ? t("web.updates.core_written", args) : t("web.updates.core_written_off", args)));
  }
  const stale = staleVersion(), testing = testingVersion();   // "tests running" — in the check line above, unless it shows an error
  if (stale) wrap.append(el("p", "hint warn", stale[0].toUpperCase() + stale.slice(1)));
  if (testing && failed) wrap.append(el("p", "hint", testing[0].toUpperCase() + testing.slice(1)));
  if (!code.upstream && code.unpublished) {
    const n = unpublishedCount(code);
    wrap.append(el("p", "hint", n ? t("web.updates.unpublished_changes", { count: n }) : t("web.updates.all_published")));
  }
  const link = el("a", null, t("web.updates.history"));
  link.href = "#/updates";
  wrap.append(el("p", "hint"), link, el("h3", "day", t("web.updates.whats_new")), ...changesPlain(code.changes || []));
  return wrap;
}

// the full count; the server truncates the list, and an old server does not send the count — then count the list
function unpublishedCount(code) {
  return Number.isInteger(code.unpublished_count) ? code.unpublished_count : code.unpublished.length;
}

export function updatesView() {
  loadAbout();
  const code = fullCode(), ups = fullUpdates().reverse();
  const wrap = el("div", "sched");
  const h = el("div", "head");
  const back = el("button", "btn quiet back", t("web.nav.all_sessions"));
  back.type = "button";
  back.addEventListener("click", toList);
  h.append(back, el("h2", "page-title", t("web.updates.title")), el("span", "pill", shortVersion(code)));
  const f = el("div", "feed"), inner = el("div", "feed-inner upd-list");
  // no paragraph about how updates work: the last check and its buttons ("Check for updates", "Update") say it
  if (code.upstream) inner.append(updateCheck());
  else if (!code.layer) inner.append(el("p", "hint", t("web.updates.hint_shared")));
  if (!code.upstream && code.unpublished) {
    const n = unpublishedCount(code);
    inner.append(el("h3", "day", n ? t("web.updates.unpublished", { count: n }) : t("web.updates.all_published")));
    if (code.published_at) inner.append(el("p", "hint", t("web.updates.last_published", { when: when(code.published_at) })));
    if (code.release) inner.append(el("p", "hint", t("web.updates.last_release", { tag: code.release.tag }) + (code.release.at ? " · " + when(code.release.at) : "")));
    const ul = el("ul", "jobs");
    for (const c of code.unpublished) ul.append(el("li", "job-note", c));
    inner.append(ul);
    if (about && n > code.unpublished.length) inner.append(el("p", "hint", t("web.updates.unpublished_more", { count: n - code.unpublished.length })));
  }
  // the check line above already says a version on trial, and says the restart while the check itself asks for it: a
  // heading would say it twice. After a background update the next check says "latest version" while the hub still
  // runs the old code — then only the heading says the restart. A hub with no upstream has no check line; a line that
  // shows an error says neither, so the headings do
  const stale = staleVersion(), testing = testingVersion(), c = (S.snap || {}).update_check;
  const saidRestart = code.upstream && lineSaysRestart(c, checkWaiting(c), testing);
  const twice = saidRestart && stale === t("web.updates.stale_restart");
  if (stale && !twice) inner.append(el("h3", "day warn", stale[0].toUpperCase() + stale.slice(1)));
  if (testing && (!code.upstream || failed)) inner.append(el("h3", "day", testing[0].toUpperCase() + testing.slice(1)));
  inner.append(...changesList((code.changes || []).slice(0, 2)));
  // no "Update history" heading: a space sets the past updates apart from the releases
  ups.forEach((u, k) => {
    // the app went back from a release (the owner or its watchdog), or the owner cancelled one before the restart (a
    // code layer): the line is in notes
    const back = u.why === "rolled-back" || u.why === "cancelled", latest = k === 0;
    const holds = latest && !u.ok && !caughtUpSince(u);   // a failure the hub has got past reads as the rest
    inner.append(notesBox(`${u.ok ? (u.version ? t("web.updates.upd_ok_version", { version: u.version }) : t("web.updates.upd_ok"))
      : back ? t("web.updates.upd_rolled_back", { version: u.version || "?" }) : t("web.updates.upd_fail")} · ${when(u.ts)}`,
      u.notes || [], `u/${u.seq ?? u.ts}/${u.ok ? 1 : 0}/${u.version || ""}`, holds,
      { why: !u.ok && !back ? WHY[u.why] || u.why || "" : "", now: holds, state: u.ok ? "ok" : back ? "" : "fail", cls: latest ? "upd-past" : "" }));
  });
  if (!ups.length && code.upstream) inner.append(el("p", "hint", t("web.updates.none")));
  f.append(inner);
  wrap.append(h, f);
  return wrap;
}
