// The "More" menu: dangerous and secondary actions — kept away from the main action. A real menu:
// aria-haspopup, role=menu / menuitem, arrow keys, Home/End, Esc returns focus to the button, a click outside closes it.
// The page redraws the header on every snapshot, so the open menu is remembered here, not in the node.
import { el } from "./dom.js";
import { render } from "./state.js";
import { t } from "./i18n.js";

let openKey = null;       // which menu is open
let focusIdx = -1;        // the focused item: after a redraw focus returns to it
let justOpened = false;   // the entrance animates once, not on every snapshot redraw
let bound = false;

function close(refocus) {
  const key = openKey;
  openKey = null;
  focusIdx = -1;
  render();
  if (refocus) document.querySelector(`.menu-btn[data-key="${CSS.escape(key)}"]`)?.focus();
}

function bind() {
  if (bound) return;
  bound = true;
  document.addEventListener("pointerdown", (e) => { if (openKey && !e.target.closest(".menu-wrap")) close(false); });
  // Esc with the menu open closes the menu and nothing else (otherwise it would also stop the session's turn)
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !openKey) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    close(true);
  }, true);
}

// items: {label, onClick, title?, checked?, disabled?} — an item (checked — a tick: menuitemcheckbox; disabled — visible,
// focusable and has a tooltip with the reason, but a click does nothing); {text} — an explanation without an action;
// {sep: true} — a separator; cls on any of them — the node's class (an item hidden by its style is skipped by the arrow keys)
export function moreMenu(key, items, label = t("web.menu.more"), { up = false, focusChecked = false } = {}) {
  bind();
  const wrap = el("div", "menu-wrap");
  const open = openKey === key;
  const btn = el("button", "btn quiet menu-btn", label);
  btn.type = "button";
  btn.dataset.key = key;
  btn.setAttribute("aria-haspopup", "menu");
  btn.setAttribute("aria-expanded", String(open));
  btn.addEventListener("click", () => {
    openKey = open ? null : key;
    justOpened = !open;
    focusIdx = open ? -1 : !focusChecked ? 0   // focusChecked — a pick from a list: focus goes to the chosen one
      : Math.max(0, items.filter((it) => !it.text && !it.sep).findIndex((it) => it.checked));
    render();
  });
  wrap.append(btn);
  if (!open) return wrap;
  const menu = el("div", "menu" + (up ? " up" : "") + (justOpened ? " enter" : ""));   // up — above the button (the row is at the bottom)
  justOpened = false;
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", label);
  const acts = [];
  for (const it of items) {
    const cls = it.cls ? " " + it.cls : "";
    if (it.sep) { const s = el("div", "menu-sep" + cls); s.setAttribute("role", "separator"); menu.append(s); continue; }
    if (it.text) { const t = el("div", "menu-text" + cls, it.text); t.setAttribute("role", "presentation"); menu.append(t); continue; }
    const b = el("button", "menu-item" + (it.checked !== undefined ? " check" : "") + cls, it.label);
    b.type = "button";
    b.tabIndex = -1;
    b.setAttribute("role", it.checked !== undefined ? "menuitemcheckbox" : "menuitem");
    if (it.checked !== undefined) b.setAttribute("aria-checked", String(!!it.checked));
    if (it.title) b.title = it.title;
    if (it.disabled) b.setAttribute("aria-disabled", "true");
    const at = acts.length;
    b.addEventListener("click", () => { if (it.disabled) return; close(false); it.onClick(); });
    b.addEventListener("focus", () => { focusIdx = at; });
    acts.push(b);
    menu.append(b);
  }
  menu.addEventListener("keydown", (e) => {
    const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
    if (e.key === "Tab") return close(false);   // Tab moves on through the page, the menu closes
    if (!step && e.key !== "Home" && e.key !== "End") return;
    const seen = acts.filter((b) => b.getClientRects().length), at = seen.indexOf(acts[focusIdx]);
    if (!seen.length) return;
    const to = step ? (Math.max(at, step > 0 ? -1 : 0) + step + seen.length) % seen.length : e.key === "Home" ? 0 : seen.length - 1;
    e.preventDefault();
    seen[to]?.focus();
  });
  wrap.append(menu);
  // the menu is aligned to the button's right edge; if the button wrapped to the start of the line and there is no room
  // on the left, open it to the right of the button (in a narrow window the menu went under the side panel and its items were clipped)
  requestAnimationFrame(() => {
    const pane = wrap.closest("main") || document.body;
    const m = menu.isConnected && menu.getBoundingClientRect(), p = pane.getBoundingClientRect();
    // a right-to-left page aligns it to the button's left edge (inset-inline-end): there the overflow is on the right
    if (m && (getComputedStyle(menu).direction === "rtl" ? m.right > p.right : m.left < p.left)) menu.classList.add("from-left");
  });
  // the node is not on the page yet; an item hidden by its style (the head's switches on a wide screen) cannot take the focus: the first one shown does
  if (focusIdx >= 0) queueMicrotask(() => {
    const want = acts[Math.min(focusIdx, acts.length - 1)];
    (want?.getClientRects().length ? want : acts.find((b) => b.getClientRects().length))?.focus();
  });
  return wrap;
}
