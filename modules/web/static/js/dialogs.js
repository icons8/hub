// Confirmations: put to sleep, fire, remove. One <dialog> on the page, the text is filled in.
import { $ } from "./dom.js";
import { S, RESTING, isRunner, resting, sessionName } from "./state.js";
import { banner, post } from "./api.js";
import { t } from "./i18n.js";

export function confirmDlg(title, text, okLabel, onOk, kind = "danger") {
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  const ok = $("confirm-ok");
  ok.textContent = okLabel;
  ok.className = kind ? "btn " + kind : "btn";
  const dlg = $("confirm-dlg");
  dlg.returnValue = "";
  dlg.onclose = () => { if (dlg.returnValue === "ok") onOk(); };
  dlg.showModal();
}

// the same dialog with a field (the session's number of workers): a regular button, not a "danger" one; Enter in the field acts as the confirm button
export function numberDlg(title, text, field, onOk) {
  confirmDlg(title, text, t("web.dialogs.set_ok"), onOk, "");
  $("confirm-text").append(field);
  field.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); $("confirm-ok").click(); } });
  queueMicrotask(() => { field.focus(); field.select(); });   // focus goes to the field, not to the "Keep" button (iOS)
}

export function go(name) { location.hash = name ? "#/s/" + encodeURIComponent(name) : "#/"; }
// "All sessions": the session list (a separate screen on a phone, on a wide screen the hub feed sits next to the list)
export function toList() { location.hash = "#/sessions"; }

export function askSleep(name) {
  confirmDlg(t("web.dialogs.sleep_title", { name: sessionName(name) }),
    t("web.dialogs.sleep_text"),
    // the server waits for the core's decision: a refusal is shown in the core's own words; if the core did not record it in time, say so instead of staying silent
    t("web.dialogs.sleep_ok"), async () => {
      const r = await post("/api/session/close", { session: name });
      if (r.error) banner(r.error);
      else if (r.data?.queued) banner(r.data.text);
    });
}

// the core does not put an agent to sleep until the owner fires it (pressing "sleep" on an agent did nothing)
export function askFire(name, resting) {
  confirmDlg(t("web.dialogs.fire_title", { name: sessionName(name) }),
    resting ? t("web.dialogs.fire_text_resting") : t("web.dialogs.fire_text_busy"),
    t("web.dialogs.fire_ok"), async () => { const r = await post("/api/session/fire", { session: name }); if (r.error) banner(r.error); });
}

export function askTrain(name) {
  confirmDlg(t("web.dialogs.train_title"),
    t("web.dialogs.train_text", { name: sessionName(name) }),
    t("web.dialogs.train_ok"), async () => { const r = await post("/api/session/train", { session: name }); if (r.error) banner(r.error); });
}

export function askRemove(names, sid) {
  const n = names.length;
  confirmDlg(n === 1 ? t("web.dialogs.remove_title_one", { name: sessionName(names[0]) }) : t("web.dialogs.remove_title_many", { count: n }),
    sid ? t("web.dialogs.remove_text_sid", { sid }) : t("web.dialogs.remove_text"),
    t("web.dialogs.remove_ok"), async () => {
      for (const x of names) { const r = await post("/api/session/remove", { session: x }); if (r.error) { banner(r.error); break; } }
      if (names.includes(S.selected)) go(null);
    });
}

// "Stop" was pressed — the header says "Stopping…" until the turn ends (up to 15 s): otherwise people pressed it six times
// in two seconds because the press gave no response
export const stopping = new Map();
export async function interrupt(s) {
  if (Date.now() - (stopping.get(s.name) || 0) < 15000) return;
  stopping.set(s.name, Date.now());
  const r = await post("/api/interrupt", { session: s.name });
  if (r.error) { stopping.delete(s.name); banner(r.error); }
}

// "Stop executor": the core kills its process tree at once, even mid-step, and puts it to sleep with the conversation;
// no confirmation — the work stays in its branch, and the owner's message in its field wakes it. Until the snapshot shows
// it asleep (up to 15 s) the button says "Stopping…": one press, one request; once asleep the mark is dropped, so a
// woken executor gets a working button at once
const killing = new Map();
export const STOPPABLE = ["starting", "idle", "busy", "dialog", "held"];   // the states the core stops at once (machines.py)
export const stoppingUntil = (s) => (killing.get(s.name) || 0) + 15000;
export const stoppingExecutor = (s) => Date.now() < stoppingUntil(s);
export const stoppedExecutor = (s) => killing.delete(s.name);
export async function stopExecutor(s) {
  if (stoppingExecutor(s)) return;
  killing.set(s.name, Date.now());
  const r = await post("/api/session/stop", { session: s.name });
  if (r.error) { killing.delete(s.name); banner(r.error); }
}

// what can be done to the session itself: stop its turn or the executor, put it to sleep, dismiss an agent, remove a
// resting one from the list. One list for the header's "More" menu and the session row's menu (right click, long press);
// a system agent always exists: it cannot be dismissed, put to sleep or removed
export function stopItems(s) {
  const out = [];
  const runner = isRunner(s);
  if (["busy", "dialog"].includes(s.state) && Date.now() - (stopping.get(s.name) || 0) >= 15000)
    out.push({ label: runner ? t("web.main.stop_turn") : t("web.main.stop"), title: t("web.main.stop_hint"), onClick: () => interrupt(s) });
  if (runner && STOPPABLE.includes(s.state) && !stoppingExecutor(s))
    out.push({ label: t("web.main.stop_executor"), title: t("web.main.stop_executor_hint"), danger: true, onClick: () => stopExecutor(s) });
  return out;
}
export function lifeItems(s) {
  const fixed = s.kind === "hub" || s.system, out = [];
  if (fixed) return out;
  if (s.role === "agent") out.push({ label: t("web.main.fire"), title: t("web.main.fire_hint"), onClick: () => askFire(s.name, RESTING.includes(s.state)) });
  else if (!RESTING.includes(s.state)) out.push({ label: t("web.main.sleep"), title: t("web.main.sleep_hint"), onClick: () => askSleep(s.name) });
  // a parked session (idle, no process) too: the core accepts its deletion; one waiting for you — no, same as a sleeping one
  if (resting(s) || (s.parked && s.status === "idle")) out.push({ label: t("web.main.remove"), title: t("web.main.remove_hint"), onClick: () => askRemove([s.name], s.session_id) });
  return out;
}
