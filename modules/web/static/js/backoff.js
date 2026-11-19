// The wait for the hub is a process, not an error: one neutral status "Connecting to the hub… · in N s" everywhere the
// page has nothing to show for lack of a connection (the stream banner, the phone session list, a feed that did not
// load). The pause before the next attempt grows by Fibonacci — 1, 1, 2, 3, 5, 8, 13, 21, 34, 55 s — and stays at 60 s.
import { t } from "./i18n.js";
import { OWN } from "./dom.js";

const FIB = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55], CEIL = 60;

// seconds before the attempt that follows `fails` failures in a row (0 — the first failure)
export const pauseSec = (fails) => (fails < FIB.length ? FIB[Math.max(0, fails)] : CEIL);

// one more failure of `w` ({fails, nextAt, timer} — the stream in api.js, a feed in timeline.js): the next attempt `fn`
// after the pause; a pending attempt is replaced, not left running beside it
export function failedOnce(w, fn) {
  const ms = pauseSec(w.fails || 0) * 1000;
  w.fails = (w.fails || 0) + 1;
  w.nextAt = Date.now() + ms;
  clearTimeout(w.timer);
  w.timer = setTimeout(() => { w.timer = null; fn(); }, ms);
}

// the status text; `at` — when the next attempt starts (ms); no time left or unknown — an attempt is under way
export function waitText(at) {
  const sec = Math.ceil((at - Date.now()) / 1000);
  return sec > 0 ? t("web.api.no_conn_in", { count: sec }) : t("web.api.no_conn");
}

// the status into `node`, which a press retries: the seconds are for the eye only (aria-hidden — in a live region a
// screen reader would read them every second), a screen reader gets the words once — the status already there keeps
// its words and only gets the new time; the control's name stays "Retry" while the text counts down
export function showWait(node, at) {
  let eye = node.querySelector(":scope > [data-conn-at]");
  if (!eye || !node.querySelector(":scope > .sr")) {
    const ear = document.createElement("span");
    eye = document.createElement("span");
    eye.setAttribute("aria-hidden", "true");
    ear.className = "sr";
    ear.textContent = t("web.api.no_conn");
    node.replaceChildren(eye, ear);
  }
  eye.dataset.connAt = at;
  eye.textContent = waitText(at);
  node.setAttribute("aria-label", t("web.main.feed_retry"));
}

// a node with data-conn-at counts down by itself; whoever replaces its text takes it away with the text
setInterval(() => {
  for (const n of document.querySelectorAll(`[data-conn-at]${OWN}`)) n.textContent = waitText(Number(n.dataset.connAt));
}, 1000);
