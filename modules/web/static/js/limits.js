// Claude account limits, as in the Claude Code status line: a window is a bar, its share used and when it resets.
// The data is the latest rate_limit_event of any session (core: account.limits); no sessions yet — no line.
import { el, time } from "./dom.js";
import { clock } from "./clock.js";
import { S } from "./state.js";
import { locale, t } from "./i18n.js";
import { countsDown, pctNum, pctText } from "./limitpct.js";

const NAMES = { five_hour: t("web.limits.name_five_hour"), seven_day: t("web.limits.name_week"),
  seven_day_opus: t("web.limits.name_week_opus"), seven_day_sonnet: t("web.limits.name_week_sonnet") };
const ORDER = Object.keys(NAMES);

function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return t("web.limits.at_time", { time: time(ts) });
  return clock(ts, locale, { day: "numeric", month: "short" });
}

const SHORT = { five_hour: t("web.limits.short_five_hour"), seven_day: t("web.limits.short_week"),
  seven_day_opus: t("web.limits.short_week_opus"), seven_day_sonnet: t("web.limits.short_week_sonnet") };

// a Codex window of another length (hosts/codex_limits: codex_<N>m) — by its length: "1 h", "30 min", "2 d"
function codexWin(name) {
  const m = /^codex_(\d+)m$/.exec(name);
  if (!m) return null;
  const mins = Number(m[1]);
  return mins % 1440 === 0 ? t("web.limits.cx_f_days", { n: mins / 1440 })
    : mins % 60 === 0 ? t("web.limits.cx_f_hours", { n: mins / 60 }) : t("web.limits.cx_f_minutes", { n: mins });
}

// compact, no bars, in the status line under the input: just the 5-hour window and its percentage, then the week and
// its percentage: "5 h 47% · week 68%"; the reset time goes in the tooltip (Codex: the share left, "left" in the tooltip)
export function limitsLine() {
  const lim = S.snap?.limits;
  const wins = Object.entries(lim?.windows || {}).filter(([, w]) => typeof w.used === "number");
  if (!wins.length) return null;
  wins.sort(([a], [b]) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99));
  const line = el("span", "limline");
  // the limits of a Codex sign-in (the fact's engine) are not Claude's
  line.setAttribute("aria-label", t(lim.engine === "codex" ? "web.limits.cx_f_label" : "web.limits.label"));
  const now = Date.now() / 1000;
  const down = countsDown(lim), left = t("web.limits.left");   // Codex: the share left, as its /status says; the word — in the tooltip
  if (S.snap?.accounts?.on) {
    // Claude accounts are on: show whose limit this is; a click opens the limits of all accounts
    const name = S.snap.account?.active;
    if (name) {   // the pool account's email goes next to the name; on a narrow screen it does not fit with the percentages — name only
      const acc = el("span", "limseg limacc", name);
      const email = (S.snap.accounts.pool || []).find((a) => a.name === name)?.email;
      if (email) acc.append(el("span", "limmail", ` (${email})`));
      line.append(acc);
    }
    line.classList.add("limlink");
    line.setAttribute("role", "link");
    line.tabIndex = 0;
    line.title = t("web.limits.all_accounts");
    const open = () => { localStorage.setItem("hub.settingsTab", "accounts"); location.hash = "#/settings"; };
    line.addEventListener("click", open);
    line.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  }
  for (const [name, w] of wins) {
    // the reset has passed (the core's boundary, reset_passed): a measurement of the previous window - not "0%", but "—"
    // and the words of "Settings → Accounts" in the tooltip
    const gone = w.resets && w.resets <= now;
    const used = gone ? 0 : Math.round(w.used * 100);
    const seg = el("span", "limseg");
    seg.dataset.level = used >= 80 ? "high" : used >= 60 ? "warn" : "ok";   // from 80% the number turns red
    const pct = pctText(used, down, left);
    seg.title = `${NAMES[name] || codexWin(name) || name}: ` + (gone ? t("web.accounts.window_reset")
      : pct + (w.resets ? t("web.limits.resets", { when: when(w.resets) }) : ""));
    seg.append(el("span", "limname", SHORT[name] || codexWin(name) || name), el("span", "limpct", gone ? "—" : pctNum(used, down)));
    line.append(seg);
  }
  if (lim.status === "rejected") line.append(el("span", "limseg limstop", t("web.limits.exhausted")));
  // limit-saving mode (core: budget in the snapshot, only with mini-hubs): new executors wait, except for your tasks;
  // with the budget gate off (gate: false) they do not wait for your word: at the pool threshold — for the window
  // reset, in economy mode — for no one
  const b = S.snap?.budget;
  if (b) {
    const owner = b.mode === "owner", word = b.gate !== false;
    const head = owner ? (word ? t("web.limits.budget_owner") : t("web.limits.budget_owner_reset"))
      : t("web.limits.budget_economy");
    const seg = el("span", "limseg " + (owner ? "limstop" : "limeco"), head +
      (b.queued ? ` · ${t("web.limits.budget_queued", { n: b.queued })}` : ""));
    seg.title = (owner ? (word ? t("web.limits.budget_owner_title") : t("web.limits.budget_owner_reset_title"))
      : (word ? t("web.limits.budget_economy_title") : t("web.limits.budget_economy_cheap_title"))) +
      (b.why ? ` (${b.why})` : "") + (b.until ? `; ${t("web.limits.budget_reset", { when: when(b.until) })}` : "") +
      (b.queued ? `; ${t("web.limits.budget_queued", { n: b.queued })}` : "");
    line.append(seg);
  }
  return line;
}

