// The Clean theme, the core's default look (MIT, its author's LICENSE is next to this file): colors and font are in palette.css,
// the layout in clean.css, the icons are set by js/themes.js; everything acts only while Clean is chosen — the th-clean class
// on <html>, which only js/themes.js sets (th-clean-dark — the dark one). main.js registers this code before the first render
// through the plugin API (js/ext.js registerBuiltIn); its strings are the core's (web.clean.*).
//
// What the theme cannot express in CSS is done here — and only while it is selected:
// - "Hub" and "Core" in the feed get the hub's icon: the byline is marked with a class, clean.css sets the picture;
// - tab counters "All · 38" — as a separate badge;
// - limits — at the bottom of the sidebar (the sidebar.footer place), from the core snapshot; with the "Usage bars" option — window
//   bars as in the native app (usage.js);
// - the session row caption the theme hides — in the row tooltip;
// - "Waiting for you" — as the number of questions (red counter), chevrons ▸ ▾ — as a rotated Forma icon;
// - theme icons — as a mask in the text color (white on red and on a dark button);
// - search as in Slack: a "clear" cross, an empty result — what was searched and a "Clear search" button;
// - every author in the feed has a picture: "Hub" and "Core" — their own, a session — its character (clean/faces.js) by its icon,
//   else one by its name; in "replied to your message" — the replier's picture instead of ●;
// - a "Hide list" button next to search and "Show list" in the corner of a hidden list — they press the core's own strip;
// - "New group", "Sort into groups" and "Restore default groups" — each a full-width row right under the groups; the list edge fades on scroll;
// - per the DS mockup: placeholders "Find…" and "A task or a question for the hub…", "·" in "working" rows and under the field — as a separate
//   gray mark, a number on the executors badge, "Send" visible even in an empty field with voice;
// - the number of open tasks — as a badge on the "Board" tab (CSS hides the core's summary line from the header); the clock icon in the
//   schedule badge — as a mask in the text color; a session with an icon image in the feed gets a gray tile with a small icon;
// - "Reply" as an icon on the floating toolbar (as in Slack): the tooltip is the button's words, the " · " before it is a separate mark
//   the theme hides; an expanded conversation between sessions collapses with a chevron labelled "Collapse"; list context
//   menus without a heading get a menu name for screen readers;
// - voice recording from the keyboard: Enter stops and sends, a double Esc cancels, a single Esc leaves the recording alone.
// - the "Detailed list" of sessions (an option in "Settings → Look", off by default): the second line holds the start of the session's
//   last message (like a messenger preview), on the right — the time of last activity; the open session's row is a card (rowDetail);
// - bottom row of the composer card (+, status chips, plugin buttons, camera, mic, send): room is shared by the measured
//   width and nothing overlaps (fit.js).
import { clock } from "../js/clock.js";
import { mountFit } from "./fit.js";
import { AGO, byRecent, detailOf, liveLine, sessionIndex } from "./rows.js";
import { WORDS as USAGE_WORDS, emailSteps, usageModel } from "./usage.js";
import { countsDown, pctNum, pctText } from "../js/limitpct.js";
import { characterOf, faceByName, hubCharacter, loadTags } from "./faces.js";

const on = () => document.documentElement.classList.contains("th-clean");

export default function register(hub) {
  const fit = mountFit(on);   // composer bottom row: room is shared by the real width (fit.js)
  const T = (k, fb, vars) => { try { const v = hub.t?.(k, vars); return v && v !== k ? v : fb; } catch { return fb; } };
  // sessions by name: every row and every byline asks for its session on each redraw, so the snapshot's sessions are put into
  // maps once per sessions array (the core hands out a new array only when a session changed), not scanned per row
  const indexes = new WeakMap();
  const sessions = () => {
    const list = hub.snapshot?.()?.sessions || [];
    let x = indexes.get(list);
    if (!x) { x = sessionIndex(list); x.hub = list.find((s) => s.kind === "hub") || null; indexes.set(list, x); }
    return x;
  };
  const names = () => ({ hub: T("web.who.hub", "Hub"), core: T("web.who.core", "Core"), owner: T("web.who.owner", "Owner") });

  // author bylines in the feed: "Hub" and "Core" get their own avatars
  const tagBylines = (root) => {
    if (!on()) return;
    const n = names();
    for (const b of root.querySelectorAll?.(".byline:not([data-clean])") || []) {
      const who = b.querySelector(":scope > b")?.textContent?.trim().toLowerCase();
      b.dataset.clean = who === n.hub.toLowerCase() ? "hub" : who === n.core.toLowerCase() ? "core" : "";
      if (who === "owner" || who === n.owner.toLowerCase()) b.dataset.cleanOwner = "";   // the core sends "owner" in lowercase; like "Hub" and "Schedule" — capitalized
    }
  };
  // "All · 38" → "All" and a "38" badge; the "Counts in the list" option off (the default) — the tab keeps its word alone: a count
  // of everyone is decoration next to the red "waiting for you" number on a row. The core writes the number again on each
  // redraw, the sweep takes it off again
  const splitTabs = () => {
    const bar = document.getElementById("active-bar");
    if (!bar || !on()) return;
    const counts = !!hub.option?.("counts");
    for (const b of bar.children) {
      const badge = b.querySelector(".c-n");
      if (badge) { if (!counts) b.replaceChildren(document.createTextNode(b.firstChild?.textContent || "")); continue; }
      const m = /^(.*?)\s*·\s*(\d+)$/.exec(b.textContent || "");
      if (!m) continue;
      if (!counts) { b.replaceChildren(document.createTextNode(m[1])); continue; }
      const n = document.createElement("span");
      n.className = "c-n";
      n.textContent = m[2];
      b.replaceChildren(document.createTextNode(m[1]), n);
    }
  };
  // settings tabs: the core adds " ·" to a tab's name when there is something to know there ("Modules ·", "Plugins ·"); the theme
  // shows it as a 6 dot in the warning color 4 after the name. The "·" stays in the button (a screen reader reads it), clean.css
  // hides it; the core redraws the tabs, the sweep puts the dot back
  const tabMarks = () => {
    for (const b of document.querySelectorAll("[data-settings-tab]")) {
      const tx = b.firstChild;
      if (b.childNodes.length !== 1 || tx?.nodeType !== 3) continue;
      const m = /^(.*?)\s*·$/.exec(tx.data);
      if (!m) continue;
      const dot = document.createElement("span");
      dot.className = "clean-att";
      dot.textContent = " ·";
      b.replaceChildren(document.createTextNode(m[1]), dot);
    }
  };
  // the theme hides the session row caption (what it does, what it waits for), as in the mockup — it stays in the row tooltip
  const rowTitles = () => {
    if (!on()) return;
    for (const r of document.querySelectorAll("#list .srow")) {
      const nm = r.querySelector(".nm"), lb = r.querySelector(".lb")?.textContent?.trim();
      const t = [nm?.title || nm?.textContent?.trim(), lb, r.dataset.cleanTip].filter(Boolean).join("\n");
      if (t && r.title !== t) r.title = t;
    }
  };
  // "Waiting for you" — as a red counter, as in the mockup: how many questions await an answer (card questions and permission requests)
  const asks = (s) => (s.pending || []).reduce((n, r, i) => n + (r.tool === "AskUserQuestion" ? r.input?.questions?.length || 1 : i === 0 ? 1 : 0), 0)
    + (s.shown || []).reduce((n, q) => n + (q.input?.questions?.length || 1), 0);
  const waitCounts = () => {
    const tags = document.querySelectorAll("#list .srow .tag:not(.soft)");
    if (!tags.length) return;
    const by = sessions().byName;
    for (const tag of tags) {
      const nm = tag.closest(".srow")?.querySelector(".nm");
      const s = by.get((nm?.title || nm?.textContent || "").split(" — ")[0].trim());
      const n = String((s && asks(s)) || 1);
      if (tag.dataset.n !== n) tag.dataset.n = n;
    }
  };
  // the core's chevrons are the characters ▸ ▾: the theme draws one Forma icon and rotates the expanded one
  const chevrons = () => {
    for (const c of document.querySelectorAll(".chev, .chat-chev")) {
      const d = c.textContent.includes("▾") ? "1" : "";
      if ((c.dataset.down || "") !== d) { if (d) c.dataset.down = d; else c.removeAttribute("data-down"); }   // closed — no attribute, as the core builds it
    }
  };
  // theme icons are images with the color inside: into a mask, so the color comes from the text (a white cross on a red header)
  const maskIcons = () => {
    for (const k of document.querySelectorAll(":is(.icon, .chip-glyph).kit-img:not([data-clean-mask])")) {
      const url = k.style.backgroundImage;
      if (!url || url === "none") continue;
      k.dataset.cleanMask = "";
      k.style.webkitMaskImage = k.style.maskImage = url;
      k.style.backgroundImage = "none";
    }
  };
  const unmaskIcons = () => {
    for (const k of document.querySelectorAll(":is(.icon, .chip-glyph).kit-img[data-clean-mask]")) {
      k.style.backgroundImage = k.style.maskImage;
      k.style.webkitMaskImage = k.style.maskImage = "";
      delete k.dataset.cleanMask;
    }
  };
  // search as in Slack: a "clear" cross in the field; nothing found is the core's one line (clean.css), no icon, hint or button —
  // the cross and Esc clear the field
  const W = { clear: T("web.clean.clear_search", "Clear search") };
  const clearSearch = () => {
    const i = document.getElementById("filter");
    if (!i) return;
    i.value = "";
    i.dispatchEvent(new Event("input", { bubbles: true }));
    i.focus();
  };
  const searchBox = () => {
    const box = document.querySelector(".search-row label.search");
    if (!box || box.querySelector(".clean-x")) return;
    const x = hub.el("button", "clean-x");
    x.type = "button";
    x.title = W.clear;
    x.setAttribute("aria-label", W.clear);
    x.addEventListener("click", (e) => { e.preventDefault(); clearSearch(); });
    box.append(x);
  };
  // hide/show the list with an icon button (as in ChatGPT and Claude): next to search, and for a hidden list — in the page
  // corner. The hiding itself is native: the core hides the list with the .grip.left divider strip (click or Enter), remembers the choice
  // in localStorage hub.leftFolded and keeps a "›" strip by the hidden list; the button presses that strip
  const FOLD = { hide: T("web.clean.hide_list", "Hide the session list"), show: T("web.clean.show_list", "Show the session list") };
  const toggleList = (e) => {
    const g = document.querySelector(".grip.left");
    if (!g) return;
    g.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    // from the keyboard, focus moves to the button that is now in view: it can be pressed again
    const folded = document.getElementById("app")?.classList.contains("left-folded");
    if (e?.detail === 0) document.querySelector(folded ? ".clean-fold-show" : ".search-row .clean-fold")?.focus({ preventScroll: true });
  };
  const foldButton = (cls, label) => {
    const b = hub.el("button", cls);
    b.type = "button";
    b.title = label;
    b.setAttribute("aria-label", label);
    b.addEventListener("click", toggleList);
    return b;
  };
  // the corner button of a hidden list stands on the header's first row, whatever stands above the header (a plugin's topbar strip, a
  // banner of the page): a fixed top would sit over the strip's icons while the title goes lower. The top row of the page (main > .top)
  // is watched: a strip that fills, wraps or is hidden without a redraw moves the header too. Only while the list is hidden;
  // unhiding it (hub-sidebar-changed) measures again
  let topWatched = null;
  const topRO = typeof ResizeObserver === "function" ? new ResizeObserver(() => { if (on()) foldTop(); }) : null;
  const foldTop = () => {
    const app = document.getElementById("app");
    if (!app || !app.classList.contains("left-folded")) return;
    const row = app.querySelector(":scope > main > .top");
    if (topRO && row && row !== topWatched) { topWatched = row; topRO.disconnect(); topRO.observe(row); }
    const head = app.querySelector(":scope > main > .head");
    const top = head?.offsetHeight ? Math.round(head.getBoundingClientRect().top - app.getBoundingClientRect().top + 10) : 0;
    const now = app.style.getPropertyValue("--clean-fold-top");
    if (top > 0 && top !== 18) { if (now !== `${top}px`) app.style.setProperty("--clean-fold-top", `${top}px`); }
    else if (now) app.style.removeProperty("--clean-fold-top");
  };
  addEventListener("hub-sidebar-changed", () => { if (on()) requestAnimationFrame(foldTop); });
  const foldButtons = () => {
    const row = document.querySelector(".search-row"), app = document.getElementById("app");
    if (row && !row.querySelector(".clean-fold")) row.append(foldButton("clean-fold", FOLD.hide));
    if (app && !app.querySelector(":scope > .clean-fold-show")) app.append(foldButton("clean-fold-show", FOLD.show));
  };
  // the character of an author: that of its icon, else of whoever raised it, else one by its name (characterOf: the one rule of
  // the list and the feed); "" — an emoji of their own stays an emoji. A name that is no session's — one by the name
  const sessionOf = (name) => sessions().byName.get(name);
  const characterFor = (name) => { const s = sessionOf(name); return s ? characterOf(s, sessions().byName) : faceByName(name); };
  // the character on a node (data-clean-av + --clean-av), or taken off it when there is none; asked on every pass: the author
  // or the icon of a node the core kept may have changed
  const putFace = (n, url) => {
    if ((n.dataset.cleanAv || "") === url) return;
    if (url) { n.dataset.cleanAv = url; n.style.setProperty("--clean-av", `url("${url}")`); }
    else { delete n.dataset.cleanAv; n.style.removeProperty("--clean-av"); }
  };
  // under the character: the session's emoji or the first letter of the name, seen while there is no character
  const faceText = (name) => sessionOf(name)?.icon || Array.from(name)[0].toUpperCase();
  // a byline whose picture the core drew (kit-img: the theme's picture of the declared icon): the character goes over it; the core's own
  // picture is kept in data-clean-kit and goes back when the author has no character any more
  const setKit = (av, url) => {
    if (url) {
      if (!("cleanKit" in av.dataset)) av.dataset.cleanKit = av.style.backgroundImage;
      const v = `url("${url}")`;
      if (av.style.backgroundImage !== v) av.style.backgroundImage = v;
    } else if ("cleanKit" in av.dataset) {
      av.style.backgroundImage = av.dataset.cleanKit;
      delete av.dataset.cleanKit;
    }
  };
  const avatars = () => {
    // an author's byline shows the character of the list's row (characterOf): the core draws the picture of the declared icon
    // (kit-img), the list puts the owner's choice and a specialist's role before it — the row's character replaces the core's
    // picture. "" (an emoji of the owner's own) — the core's node stays as it is: a picture the theme put there before goes back.
    // The Hub's byline follows the Hub's row (hubCharacter): its character, else its emoji, else the hub's icon (data-clean-hub
    // on the byline tells clean.css which of the three). Core's own byline keeps the hub's icon
    const hubSession = sessions().hub;
    for (const av of document.querySelectorAll(".byline .av")) {
      const b = av.closest(".byline");
      const who = b.querySelector(":scope > b")?.textContent?.trim();
      if (b.dataset.clean === "core") continue;
      const isHub = b.dataset.clean === "hub" && !!hubSession;
      if (isHub) {
        const u = hubCharacter(hubSession);
        put(b, "cleanHub", u ? "head" : (hubSession.own_icon || hubSession.icon) ? "emoji" : "mark");
        if (!av.classList.contains("kit-img")) { putFace(av, u); continue; }
        setKit(av, u);
        continue;
      }
      if (b.dataset.clean === "hub") continue;   // no hub session in the snapshot yet: the stylesheet's head
      const url = who ? characterFor(who) : "";
      if (av.classList.contains("kit-img")) { setKit(av, url); continue; }
      putFace(av, url);
    }
    // "hub-dev replied to your message": the name in bold, the replier's face in the left column instead of ●. The name is the
    // first word only where it is a session's name (the order of the words is the language's)
    for (const box of document.querySelectorAll(".row > .b > .reply-box:not([data-clean])")) {
      const meta = box.querySelector(":scope > .meta"), first = meta?.firstChild;
      const m = first?.nodeType === 3 && /^(\S+)(\s[\s\S]*)$/.exec(first.nodeValue);
      if (!m || !sessionOf(m[1])) continue;
      box.dataset.clean = m[1];
      first.replaceWith(hub.el("b", "clean-who", m[1]), document.createTextNode(m[2]));
      box.closest(".row").querySelector(":scope > .g")?.replaceChildren(hub.el("span", "clean-av"));
    }
    for (const box of document.querySelectorAll(".row > .b > .reply-box[data-clean]")) {
      const a = box.closest(".row").querySelector(":scope > .g > .clean-av"), name = box.dataset.clean;
      if (!a) continue;
      const text = faceText(name);
      if (a.textContent !== text) a.textContent = text;
      putFace(a, characterFor(name));
    }
  };
  // "Board · 3": the number of open tasks on the tab — the core's summary line no longer sits under the header
  const boardCount = () => {
    const head = document.querySelector("main > .head"), name = head?.querySelector("h2")?.title;
    const snap = hub.snapshot?.(), tab = head?.querySelector(".seg")?.lastElementChild;
    if (!name || !tab || !(snap?.directions || []).includes(name)) return;
    const n = (snap.direction_tasks || {})[name]?.open || 0, now = tab.querySelector(".ce-n");
    if (!n) { now?.remove(); return; }
    if (!now) tab.append(hub.el("span", "ce-n", String(n)));
    else if (now.textContent !== String(n)) now.textContent = String(n);
  };
  // the board's own line in place of the board (boards.js): "Loading the board…" or the error the hub gave. The theme draws three
  // dots by default; any line but the loading one is an error and gets the error mark. Read by the core's own string, so the words
  // stay the core's
  const boardHint = () => {
    for (const p of document.querySelectorAll(".boards-body:not(:has(> .board)) > p.hint")) {
      const kind = p.textContent.trim() === T("web.boards.loading", "\0") ? "" : "error";
      if ((p.dataset.cleanKind || "") !== kind) p.dataset.cleanKind = kind;
    }
  };
  // "In background · 19 min": the lines under the header that the core puts in .head-meta (background work, mini-hub notes) are hidden
  // by CSS — a one-line header; the same goes into a chip next to the status, the full text in the tooltip (data-tip: from the keyboard too).
  // The direction summary (.dir-recap) does not go into the chip — it is on the "Board"
  const bgLabel = (bgs) => {
    if (bgs.length > 1) return T("web.clean.bg_many", `${bgs.length}`, { count: bgs.length });
    const m = Math.floor((Date.now() / 1000 - bgs[0].since) / 60);
    return m < 1 ? T("web.clean.bg", "In background") : m < 60 ? T("web.clean.bg_min", `${m}`, { count: m })
      : T("web.clean.bg_h", `${Math.floor(m / 60)}`, { count: Math.floor(m / 60) });
  };
  const headNote = () => {
    const head = document.querySelector("main > .head"), now = head?.querySelector(":scope > .clean-bg");
    const extra = [...(head?.querySelectorAll(":scope > .head-meta > :not(.dir-recap, .sep)") || [])];
    if (!head || !extra.length) { now?.remove(); return; }
    const name = head.querySelector("h2")?.title;
    const bgs = sessions().byName.get(name)?.background || [];
    const live = [...bgs].sort((a, b) => a.since - b.since);
    const label = live.length && extra[0].classList.contains("bg-line") ? bgLabel(live) : extra[0].textContent.trim();
    const tip = extra.map((x) => x.textContent.trim()).join("\n");
    let chip = now;
    if (!chip) {
      chip = hub.el("span", "chip clean-bg");
      chip.tabIndex = 0;
      chip.setAttribute("role", "note");
      chip.append(hub.el("span", "clean-bg-t"));
    }
    const t = chip.firstElementChild;
    if (t.textContent !== label) t.textContent = label;
    if (chip.dataset.tip !== tip) { chip.dataset.tip = tip; chip.setAttribute("aria-label", tip.replace(/\n/g, ". ")); }
    const pill = head.querySelector(":scope > .pill");
    if (pill && chip.previousElementSibling !== pill) pill.after(chip);
  };
  // "Brief | Detailed" and "Feed | Board" are hidden by CSS in a narrow header (@container hd); their items in "More" are the core's
  // (.head-fold-view, .head-fold-board, main.js), CSS shows them at the same widths. Here only the switches are told apart for that CSS
  const headFold = () => {
    const head = document.querySelector("main > .head");
    if (!head) return;
    for (const s of head.querySelectorAll(":scope > .seg:not(.clean-view, .clean-board, .tabs)")) {
      s.classList.add(s.matches(".board-switch") ? "clean-board" : "clean-view");
    }
    const menu = head.querySelector(".menu-wrap:not(.clean-tabs) > .menu");
    if (!menu || menu.querySelector(".clean-fold-i")) return;
    // on a phone "More" is a sheet from the bottom with a title: whose session it is
    const title = head.querySelector("h2")?.textContent.trim() || "";
    if (menu.dataset.cleanTitle !== title) menu.dataset.cleanTitle = title;
    // "Stop the executor" on a phone is an item of the sheet, not a button in the header row: the same way, a click on the
    // core's hidden button; a disabled one ("Stopping…") is a disabled item. It stands after the core's fold separator
    // (the switches' items above it are the core's)
    const stopExec = head.querySelector(":scope > .btn.stop-exec");
    if (stopExec && getComputedStyle(stopExec).display === "none") {
      const it = hub.el("button", "menu-item clean-fold-i", stopExec.textContent.trim());
      it.type = "button";
      it.tabIndex = -1;
      it.setAttribute("role", "menuitem");
      it.title = stopExec.title;
      if (stopExec.disabled) it.setAttribute("aria-disabled", "true");
      else it.addEventListener("click", () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); stopExec.click(); });
      const sep = menu.querySelector(":scope > .menu-sep.head-fold-view");
      if (sep) sep.after(it); else menu.append(hub.el("div", "menu-sep"), it);
    } else {
      // nothing to add: a hidden mark, so the next sweep does not look again until the core redraws the menu
      const mark = hub.el("span", "clean-fold-i");
      mark.hidden = true;
      menu.append(mark);
    }
  };
  // Settings: the section bar (.seg.tabs) is one row in the header, which does not wrap here; when it does not fit the column it turns into
  // a "☰" button with the list of sections under it. Fit is measured, not guessed from a breakpoint: the bar's width follows the language.
  // The core's bar stays in the markup (hidden): a pick clicks its button, so the core itself switches the section. The core redraws
  // the header on every snapshot, so the open list is remembered here (tabsOpen) and the sweep builds it again
  // tabsFocus — where focus must go now (an action: "btn" or "item", the ticked one); tabsAt — where it was last (null, "btn" or an item's
  // index): the redraw drops focus to <body>, and it goes back there on its own
  // tabsKept — the button with its list: the same nodes go into every new header (a list built anew each time replayed its fade-in
  // and replaced the items under the pointer on every snapshot: "it twitches when I move the mouse over it")
  let tabsOpen = false, tabsFocus = null, tabsAt = null, tabsWatch = () => {}, tabsKept = null;
  const tabsOff = () => {
    for (const h of document.querySelectorAll("main > .head.clean-tabs-fold")) h.classList.remove("clean-tabs-fold");
    for (const w of document.querySelectorAll(".clean-tabs")) w.remove();
    tabsKept = null;
    tabsOpen = false;
    tabsFocus = tabsAt = null;   // not in Settings, or it all fits: nothing stays open or waits for focus
  };
  const tabsClose = (refocus) => {
    tabsOpen = false;
    tabsAt = null;
    tabsFocus = refocus ? "btn" : null;
    tabsFold();
  };
  const tabsRefocus = (btn, items) => {
    const want = tabsFocus || (document.activeElement === document.body ? tabsAt : null);
    tabsFocus = null;
    if (want === null) return;
    const to = want === "item" ? items?.find((x) => x.getAttribute("aria-checked") === "true") || items?.[0]
      : typeof want === "number" ? items?.[Math.min(want, (items?.length || 1) - 1)] : null;
    (to || btn).focus({ preventScroll: true });
  };
  const tabsFold = () => {
    const head = document.querySelector("main > .head"), bar = head?.querySelector(":scope > .seg.tabs");
    const cs = head && getComputedStyle(head);
    if (!bar || cs.containerType !== "inline-size") { tabsOff(); return; }   // a phone's header wraps (no container there): the bar wraps with it
    tabsWatch(head);
    let wrap = head.querySelector(":scope > .clean-tabs");
    head.classList.remove("clean-tabs-fold");   // measure with the bar shown
    const gap = parseFloat(cs.columnGap) || 0;
    const kids = [...head.children].filter((k) => k !== wrap && getComputedStyle(k).display !== "none");
    const need = kids.reduce((n, k) => n + (k.matches("h2") ? k.scrollWidth : k.offsetWidth), 0) + gap * (kids.length - 1);
    const room = head.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    if (need <= room) { tabsOff(); return; }
    head.classList.add("clean-tabs-fold");
    const tabs = [...bar.children], label = T("web.settings.sections", "Settings sections");
    const cur = tabs.find((b) => b.getAttribute("aria-pressed") === "true");
    if (!wrap) {
      wrap = tabsKept || hub.el("div", "menu-wrap clean-tabs");
      tabsKept = wrap;
      bar.before(wrap);
    }
    let btn = wrap.firstElementChild;
    if (!btn) {
      btn = hub.el("button", "btn quiet menu-btn clean-tabs-btn");
      btn.type = "button";
      btn.setAttribute("aria-haspopup", "menu");
      btn.append(hub.icon("menu"));
      btn.addEventListener("click", () => { tabsOpen = !tabsOpen; tabsFocus = tabsOpen ? "item" : null; tabsFold(); });
      btn.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" && !tabsOpen) { e.preventDefault(); tabsOpen = true; tabsFocus = "item"; tabsFold(); } });
      wrap.append(btn);
    }
    btn.setAttribute("aria-expanded", String(tabsOpen));
    btn.setAttribute("aria-label", label);
    btn.title = cur ? `${label}: ${cur.textContent}` : label;
    let menu = wrap.querySelector(":scope > .menu");
    if (!tabsOpen) { menu?.remove(); tabsRefocus(btn, null); return; }
    const sig = tabs.map((b) => b.textContent + b.getAttribute("aria-pressed")).join("\n");
    if (menu?.dataset.sig === sig) { tabsRefocus(btn, [...menu.children]); return; }
    const opening = !menu;   // it fades in when it opens, not each time the items change under the pointer
    menu?.remove();
    menu = hub.el("div", opening ? "menu enter" : "menu");
    if (opening) menu.addEventListener("animationend", (e) => { if (e.target === menu) menu.classList.remove("enter"); }, { once: true });
    menu.dataset.sig = sig;
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", label);
    const items = tabs.map((b) => {
      const it = hub.el("button", "menu-item check", b.textContent);
      it.type = "button";
      it.tabIndex = -1;
      it.setAttribute("role", "menuitemradio");
      it.setAttribute("aria-checked", String(b === cur));
      // the list outlives the header it was built from: the section's button is looked up in the live bar at the click
      it.addEventListener("click", () => {
        tabsOpen = false;
        tabsFocus = "btn";
        const key = b.dataset.settingsTab;
        [...document.querySelectorAll("main > .head > .seg.tabs > button")].find((x) => x.dataset.settingsTab === key)?.click();
        tabsFold();
      });
      return it;
    });
    menu.append(...items);
    menu.addEventListener("keydown", (e) => {
      if (e.key === "Tab") { tabsClose(false); return; }   // Tab moves on through the page, the list closes
      const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
      if (!step && e.key !== "Home" && e.key !== "End") return;
      e.preventDefault();
      const at = items.indexOf(document.activeElement);
      items[step ? (Math.max(at, step > 0 ? -1 : 0) + step + items.length) % items.length : e.key === "Home" ? 0 : items.length - 1].focus();
    });
    wrap.append(menu);
    tabsRefocus(btn, items);
  };
  document.addEventListener("focusin", (e) => {
    const w = e.target.closest?.(".clean-tabs");
    tabsAt = !w ? null : e.target.matches(".menu-item") ? [...w.querySelectorAll(".menu-item")].indexOf(e.target) : "btn";
  });
  document.fonts?.addEventListener?.("loadingdone", () => { if (on()) tabsFold(); });   // the title's webfont arrives after the first measure
  // a press outside closes the list (Esc is in the one capture-phase key listener below: it closes only the list, not the turn too)
  document.addEventListener("pointerdown", (e) => {
    if (e.target.closest?.(".clean-tabs")) return;
    tabsAt = null;   // a press elsewhere is a choice of focus: the redraw does not pull it back
    if (tabsOpen) tabsClose(false);
  });
  if (typeof ResizeObserver === "function") {
    // the core draws the header anew on every snapshot: watch the live one only (a new target reports once at once — no loop: it is the same node then)
    let watched = null;
    const ro = new ResizeObserver(() => { if (on()) tabsFold(); });
    tabsWatch = (head) => { if (watched === head) return; watched = head; ro.disconnect(); ro.observe(head); };
  }
  // Thread ⋯ menu: the core's «Put to sleep» item becomes «Finish and sleep», and its confirmation dialog says the same.
  // Same item, same dialog, same close action as the native «Finish»: the session hands its work over and goes to sleep.
  // The core rebuilds the menu on every snapshot, so the rename is an idempotent pass over whatever the menu holds now
  // the core fills one #confirm-dlg for every question: rename it only while it holds the sleep question
  const finishDialog = () => {
    if (!on()) return;   // the item kept its listener after a switch to another theme: the core's dialog stays as it is
    const title = document.getElementById("confirm-title"), ok = document.getElementById("confirm-ok");
    const tpl = T("web.dialogs.sleep_title", "");
    if (!title || !ok || !tpl.includes("{name}")) return;
    const [pre, post] = tpl.split("{name}"), now = title.textContent;
    if (now.length <= pre.length + post.length || !now.startsWith(pre) || !now.endsWith(post)) return;
    title.textContent = T("web.clean.finish_title", now, { name: now.slice(pre.length, now.length - post.length) });
    if (ok.textContent === T("web.dialogs.sleep_ok", "")) ok.textContent = T("web.clean.finish_ok", ok.textContent);
  };
  const finishItem = () => {
    const menu = document.querySelector("main > .head .menu-wrap > .menu");
    const sleep = T("web.main.sleep", "");
    if (!menu || !sleep || menu.closest?.(".clean-tabs")) return;   // the Settings sections list is no thread menu
    for (const b of menu.querySelectorAll(":scope > .menu-item")) {
      if (b.textContent !== sleep) continue;
      b.textContent = T("web.clean.finish_item", sleep);
      // the core's own click handler (added first) opens the dialog synchronously; this one renames it before the first paint
      b.addEventListener("click", finishDialog);
    }
  };
  // The direction summary on the "Board": the core puts it in the header (.head-meta > .dir-recap), CSS hides it — the header does not
  // grow; here it becomes the first line of the board body and scrolls with it. A copy, not a move: the core redraws the header on every
  // snapshot; the copy's buttons press the original's buttons (it stays in the DOM, hidden)
  const boardRecap = () => {
    const body = document.querySelector("main > .boards-body"), src = document.querySelector("main > .head .head-meta > .dir-recap");
    const mine = body?.querySelector(":scope > .clean-recap");
    if (!body || !src) { mine?.remove(); return; }
    const sig = src.outerHTML;
    if (mine?.dataset.sig === sig) return;
    const copy = src.cloneNode(true);
    copy.className = "clean-recap";
    copy.dataset.sig = sig;
    copy.querySelectorAll("button").forEach((b, i) => b.addEventListener("click", () => {
      document.querySelectorAll("main > .head .head-meta > .dir-recap button")[i]?.click();
      b.disabled = true;
    }));
    if (mine) mine.replaceWith(copy); else body.prepend(copy);
  };
  setInterval(() => { if (on()) headNote(); }, 20000);   // minutes pass even without a core redraw
  const unAvatars = () => {
    for (const box of document.querySelectorAll(".reply-box[data-clean]")) {
      box.querySelector(".clean-who")?.replaceWith(document.createTextNode(box.dataset.clean));
      box.closest(".row").querySelector(":scope > .g")?.replaceChildren(document.createTextNode("●"));
      delete box.dataset.clean;
    }
  };
  // "+ Group", "Auto" and "Reset" — right under the groups (before the sleeping ones), each on its own row, in words saying what it does
  const G = { add: T("web.clean.new_group", "New group"), auto: T("web.clean.sort_groups", "Sort into groups"),
    reset: T("web.clean.reset_groups", "Restore default groups") };
  const groupTools = () => {
    const list = document.getElementById("list"), tools = list?.querySelector(":scope > .g-tools");
    if (!tools) return;
    const rest = list.querySelector(":scope > details.resting");
    if (rest && tools.nextElementSibling !== rest) list.insertBefore(tools, rest);
    for (const [sel, text] of [[".add-group", G.add], [".auto", G.auto], [".btn:not(.add-group, .auto)", G.reset]]) {
      const b = tools.querySelector(`:scope > ${sel}`);
      if (!b) continue;
      // label in a span: ellipsis works only on a block child of a flex button, so a long translation stays on one line
      let s = b.firstElementChild;
      if (s?.tagName !== "SPAN" || b.children.length !== 1 || b.childNodes.length !== 1) { s = document.createElement("span"); b.replaceChildren(s); }
      if (s.textContent !== text) s.textContent = text;
    }
  };
  // the list edge fades on scroll: a mask only on the side where there is more to scroll
  const fade = () => {
    const l = document.getElementById("list");
    if (!l) return;
    if (!l.dataset.cleanFadeOn) { l.dataset.cleanFadeOn = "1"; l.addEventListener("scroll", fade, { passive: true }); }
    const v = on() ? [l.scrollTop > 1 && "up", l.scrollTop + l.clientHeight < l.scrollHeight - 1 && "down"].filter(Boolean).join(" ") : "";
    if (v) { if (l.dataset.cleanFade !== v) l.dataset.cleanFade = v; } else if (l.hasAttribute("data-clean-fade")) delete l.dataset.cleanFade;
  };
  addEventListener("resize", fade, { passive: true });
  // the fade under the feed (behind the input field) dies out toward the end of scrolling: --clean-end on .dock (only .dock::before
  // reads it), 1 — full depth 80, 0 — feed at the bottom. On main the whole feed inherited the variable, and every scroll step near
  // the end restyled every message
  const feedEnd = () => {
    const f = document.querySelector("main > .feed"), d = f?.parentElement.querySelector(":scope > .dock");
    if (!f || !d) return;
    if (!f.dataset.cleanEndOn) { f.dataset.cleanEndOn = "1"; f.addEventListener("scroll", feedEnd, { passive: true }); }
    const k = on() ? Math.round(Math.min(1, Math.max(0, (f.scrollHeight - f.clientHeight - f.scrollTop) / 80)) * 100) / 100 : 1;
    if (d.style.getPropertyValue("--clean-end") !== String(k)) d.style.setProperty("--clean-end", String(k));
  };
  addEventListener("resize", feedEnd, { passive: true });
  // field placeholders — shorter, as in the mockup: "Find…", "A task or a question for the hub…" (the space-to-record hint is in the mic
  // tooltip); the previous ones are kept in data-clean-ph, to restore them without the theme
  const FIND = T("web.clean.find", "Find…");
  const shortHint = (ph) => { const h = ph.split(" · ")[0].trim(); return h && !h.endsWith("…") ? h + "…" : h; };
  const placeholders = () => {
    const f = document.getElementById("filter");
    // an open plugin view holds the field with its own placeholder (data-view, Plugin API 1.39): not shortened
    if (f && !f.dataset.view && f.placeholder !== FIND) { f.dataset.cleanPh = f.placeholder; f.placeholder = FIND; }
    for (const ta of document.querySelectorAll("form.composer textarea")) {
      const h = ta.placeholder && shortHint(ta.placeholder);
      if (h && h !== ta.placeholder) { ta.dataset.cleanPh = ta.placeholder; ta.placeholder = h; }
    }
  };
  const unPlaceholders = () => {
    for (const i of document.querySelectorAll("[data-clean-ph]")) {
      // the search field held by a plugin view (data-view): the original goes back when the view lets go of it
      if (i.dataset.view) i.dataset.sessionHint = i.dataset.cleanPh; else i.placeholder = i.dataset.cleanPh;
      delete i.dataset.cleanPh;
    }
  };
  // "·" between parts of a line — as a separate mark (gray 28%, 6 apart), the words — 55%: in the "working" row the core writes
  // " · 9 min 12 s" at the start of a part (and rewrites it every second), under the field — "73k tokens · " at the end
  const sep = () => hub.el("span", "clean-sep", "·");
  const separators = () => {
    for (const part of document.querySelectorAll(".row.working > .b > span.muted")) {
      const t = part.textContent;
      if (!t.startsWith(" · ")) continue;
      const tn = part.firstChild;   // the text node keeps its place (main.js tick): no tree change for the :has() rules
      if (tn?.nodeType === 3 && !tn.nextSibling) tn.nodeValue = t.slice(3); else part.textContent = t.slice(3);
      if (!part.previousElementSibling?.classList.contains("clean-sep") || part.previousSibling !== part.previousElementSibling) part.before(sep());
    }
    for (const tok of document.querySelectorAll(".stat-ctx-tok:not([data-clean])")) {
      const t = tok.textContent;
      if (!t.endsWith(" · ")) continue;
      tok.dataset.clean = "";
      tok.replaceChildren(document.createTextNode(t.slice(0, -3)), sep());
    }
  };
  // "Counts in the list" (Look): the executors badge "2 ›" — its room is held by an invisible double of the row, it needs only the number
  const forkCounts = () => {
    if (!document.documentElement.classList.contains("th-clean-counts")) return;
    for (const r of document.querySelectorAll("#list li.has-forks > .srow[data-fw]")) {
      const n = r.dataset.fw.replace(/\D/g, "");
      if (r.dataset.cleanFw !== n) r.dataset.cleanFw = n;
    }
  };
  // voice recording in progress: four running bars in the record button (styles in clean.css); the core recreates the button's content
  // when the mode changes, so the sweep puts them in
  const recBars = () => {
    for (const b of document.querySelectorAll("form.composer.rec .bar > .btn.round.stop:not(:has(> .clean-bars))")) {
      const bars = hub.el("span", "clean-bars");
      bars.append(...[0, 1, 2, 3].map(() => hub.el("i")));
      b.append(bars);
    }
  };
  // "Reply" and "Reaction" as icons: the words go to the tooltip; the " · " before it in "replied to your message" — a mark the theme hides.
  // An expanded conversation: what is left of the core's button is a chevron — labelled "Collapse". A group menu without a heading — a name
  // for screen readers
  const COLLAPSE = T("web.clean.collapse", "Collapse");
  const actions = () => {
    for (const b of document.querySelectorAll(".row .reply-btn:not([title]), .row .reply-btn-bottom:not([title]), .row > .b > .reply-btn ~ .react-add:not([title])")) {
      b.title = b.textContent.trim();
      let dot = b.previousSibling;
      if (dot?.nodeType === 1 && dot.matches(".copy-btn")) dot = dot.previousSibling;   // "Copy" stands between the dot and "Reply"
      if (b.parentElement.matches(".reply-box > .meta") && dot?.nodeType === 3 && dot.nodeValue === " · ") dot.replaceWith(hub.el("span", "clean-dot", " · "));
    }
    for (const h of document.querySelectorAll('.chat-head[aria-expanded="true"]:not([data-clean-label])')) {
      h.dataset.cleanLabel = "";
      h.setAttribute("aria-label", COLLAPSE);
      h.title = COLLAPSE;
    }
    for (const p of document.querySelectorAll(".gpop.menu:not([aria-label])")) {
      const t = p.querySelector(":scope > .gpop-t")?.textContent;
      if (t) p.setAttribute("aria-label", t);
    }
  };
  // the queue above the field (the core's strip.js puts its rows into the shared "Waiting for you" strip): here the queue rows and the
  // "Waiting for you" rows (question, permission), "Unread" and "Not delivered" move into one plate — a tab above the tokens tab.
  // Button labels go to tooltips (icons without words on a phone, "Now" instead of "Send now" on a wide screen); the core recreates
  // the whole strip when its set of rows changes
  const NOW = T("web.clean.send_now_short", "Now");
  const notRemoved = (T("web.strip.not_removed", "") || "").split("{")[0];
  const queuePlate = () => {
    for (const host of document.querySelectorAll(".strip-host")) {
      // every row of the strip goes onto the plate: "Not delivered" and "Unread" (a step runs longer than a minute, "Stopping…") too
      const rows = host.querySelectorAll(":scope > .strip > .strip-row:is([data-k=queue], [data-k=wait], [data-k=unread], [data-k=fail])");
      if (!rows.length) continue;
      let plate = host.querySelector(":scope > .clean-queue");
      if (!plate) {
        plate = hub.el("div", "clean-queue");
        plate.setAttribute("role", "region");
        plate.setAttribute("aria-label", T("web.strip.waiting", "Waiting for you"));
        host.append(plate);
      }
      for (const r of rows) {
        // a regular queue row — with the "goes when it finishes its turn" tooltip and buttons; "For your information", "Removed",
        // "Running" — their own
        const item = r.dataset.k === "queue" && !r.querySelector(":scope > button.t") && r.querySelector(":scope > .t[title]") && r.querySelector(":scope > .btn");
        // "Not removed: …" is the same queue row with "Retry", told apart only by the core's words: it is an error, as "Not delivered"
        const unremoved = r.dataset.k === "queue" && notRemoved && r.querySelector(":scope > .t")?.textContent.startsWith(notRemoved);
        r.dataset.clean = r.dataset.k === "wait" ? "wait" : r.dataset.k === "fail" || unremoved ? "fail" : r.dataset.k === "unread" ? "unread" : item ? "item" : r.matches(":has(> button.t)") ? "fyi" : r.matches(".strip-more") ? "more" : r.matches(".strip-sum") ? "sum" : "state";
        if (item) {
          for (const b of r.querySelectorAll(":scope > .btn")) {
            const now = !b.classList.contains("quiet");
            b.classList.add(now ? "clean-now" : "clean-del");
            b.title = b.getAttribute("aria-label") || b.textContent.trim();
            b.setAttribute("aria-label", b.title);
            if (now) b.dataset.short = NOW;
          }
        }
        plate.append(r);
      }
      for (const box of host.querySelectorAll(":scope > .strip:empty")) box.remove();
    }
  };
  // The composer message (clean.css) draws a red error mark by default; a line that only asks to wait for an upload, or says the
  // microphone is allowed, is not an error and gets its own mark. Read by the core's own strings, so the words stay the core's
  const NOTICE = [["wait", ["web.composer.wait_upload"]], ["ok", ["web.voice.mic_allowed_hold"]]];
  const kindOf = (text) => {
    for (const [kind, keys] of NOTICE) if (keys.some((k) => { const s = T(k, ""); return s && text.startsWith(s); })) return kind;
    return "";
  };
  const notices = () => {
    for (const p of document.querySelectorAll("form.composer > p.error-text")) {
      const kind = p.textContent ? kindOf(p.textContent.trim()) : "";
      if ((p.dataset.cleanKind || "") !== kind) p.dataset.cleanKind = kind;
    }
  };
  // theme off: the core rebuilds the strip from its rows (the signature of the row set is reset — otherwise the core leaves the plate as is)
  const unPlate = () => {
    for (const host of document.querySelectorAll(".strip-host:has(> .clean-queue)")) delete host.dataset.sig;
  };
  // The "Detailed list" view (a theme option, hub.addOptions with its themes → under the Clean rows of "Settings → Look", only
  // while Clean is chosen; the core keeps the choice in localStorage):
  // the first line is the name with the time of last activity on the right (now, 2m, 3h), the second — the project and the worktree folder
  // in monospace; no folder — the project only, no working folder — no second line. Data come from the snapshot (cwd, since,
  // state): the snapshot has no branch, so the "folder" is the worktree name from .claude/worktrees/<name>. Rows get no
  // new nodes (the core rebuilds them) — only attributes, and clean.css draws the text via attr()
  hub.addOptions({
    themes: ["core:clean", "core:clean-light", "core:clean-dark"],   // every Clean of js/themes.js BUILTIN
    options: [{ key: "detail", on: false, label: T("web.clean.detail", "Detailed session list"), hint: T("web.clean.detail_hint", "") },
      { key: "avatars", on: true, label: T("web.clean.avatars", "Chat avatars"), hint: T("web.clean.avatars_hint", "") },
      { key: "recent", on: false, label: T("web.clean.recent", "Sort by last message"), hint: T("web.clean.recent_hint", "") },
      { key: "counts", on: false, label: T("web.clean.counts", "Counts in the list"), hint: T("web.clean.counts_hint", "") },
      { key: "usage", on: false, label: T("web.clean.usage_bars", "Usage bars"), hint: T("web.clean.usage_bars_hint", "") },
      { key: "footcounts", on: false, label: T("web.clean.footcounts", "State counts under the list"), hint: T("web.clean.footcounts_hint", "") },
      { key: "narrow", on: false, label: T("web.clean.narrow", "Narrow feed"), hint: T("web.clean.narrow_hint", "") },
      { key: "waitbox", on: false, label: T("web.clean.waitbox", "Waiting for you square, as in Classic"), hint: T("web.clean.waitbox_hint", "") }],
  });
  // sorting is the core's own order hook (hub.addOrder): rows come out of the core already in place, no DOM moves; the groups the core
  // does not order (Sleeping) and the ones with a manual order (drag and drop) are fixed up in recentOrder
  const recent = () => on() && !!hub.option?.("recent");
  hub.addOrder((group, rows) => (recent() ? byRecent(rows) : rows));
  const WORDS = { now: T("web.clean.ago_now", AGO.now), m: (count) => T("web.clean.ago_m", AGO.m(count), { count }),
    h: (count) => T("web.clean.ago_h", AGO.h(count), { count }), d: (count) => T("web.clean.ago_d", AGO.d(count), { count }) };
  const rowName = (r) => {
    const nm = r.querySelector(".nm");
    const text = [...(nm?.childNodes || [])].filter((n) => n.nodeType === 3).map((n) => n.nodeValue.trim()).filter(Boolean).pop();
    return text || "";
  };
  const put = (node, key, v) => { if (v) { if (node.dataset[key] !== v) node.dataset[key] = v; } else if (key in node.dataset) delete node.dataset[key]; };
  // the session of a row: by the core's key of its item (a system session shows the page's word, "Liaison", not its name; an
  // executor under a parent — its name without the parent's), then by the name in it; the hub row carries its own label
  // ("Hub"), not the session name — it is the first row of the list
  const rowKey = (li) => li?.dataset?.s ?? li?.dataset?.m ?? li?.dataset?.f ?? "";
  const rowSession = (r, ix, firstRow) => ix.byName.get(rowKey(r.parentElement)) || ix.find(rowName(r)) || (r === firstRow ? ix.hub : null);
  // the caption the core drew in the row (.lb, hidden by the theme): without the ticking time, which the right column already has,
  // and without the badge of a direction
  const captionOf = (r) => {
    const lb = r.querySelector(".lb");
    return lb ? [...lb.childNodes].filter((n) => !(n.nodeType === 1 && n.matches(".elapsed, .avatar, .dir-badge"))).map((n) => n.textContent).join("").trim() : "";
  };
  const rowDetail = () => {
    const detail = document.documentElement.classList.contains("th-clean-detail");
    const rows = document.querySelectorAll("#list .srow:not(.nav, .queued)");
    if (!detail) { for (const r of document.querySelectorAll("#list .srow[data-clean-d]")) clearDetail(r); return; }
    const ix = sessions(), now = Date.now() / 1000, firstRow = document.querySelector("#list .sgroup.bare .srow");
    for (const r of rows) {
      const s = rowSession(r, ix, firstRow);
      const d = detailOf(s, now, WORDS, liveLine(s, captionOf(r)));
      if (!d) { if (r.dataset.cleanD) clearDetail(r); continue; }
      const txt = r.querySelector(":scope > .txt");
      put(r, "cleanD", "1"); put(r, "cleanTip", d.cwd); put(r, "cleanAgo", d.ago); put(r, "cleanTwo", d.prev || r.parentElement?.classList.contains("has-forks") ? "1" : "");   // executors: "N ›" lives at the bottom right, so the row is two-line even without a preview
      r.style.setProperty("--clean-ago-w", `${d.ago.length * 7 + 8}px`);
      if (txt) put(txt, "cleanPrev", d.prev);
    }
  };
  const clearDetail = (r) => {
    for (const k of ["cleanD", "cleanAgo", "cleanTwo", "cleanTip"]) delete r.dataset[k];
    r.style.removeProperty("--clean-ago-w");
    const txt = r.querySelector(":scope > .txt");
    if (txt) delete txt.dataset.cleanPrev;
  };
  // "Chat avatars" (a "Look" option, on by default): the row type is data-clean-k (agent / exec / other / dir / hub), the picture
  // is --clean-face, drawn by clean.css. A specialist and an executor show a character (characterOf, as in the feed): of their own
  // icon, else of the icon of whoever raised them, else one by the name; an emoji of their own stays an emoji (the core's
  // .nm > .avatar), without an icon and without a character — the first letter of the name (data-clean-letter on .txt); "Hub" — the
  // character of its own icon, else its own head (faces.js HUB_FACE), else the hub's icon (data-clean-face); a direction — a tile
  // with a Forma compass (data-clean-tile).
  // Rows get no new nodes (the core rebuilds them)
  const rowFaces = () => {
    const rows = document.querySelectorAll("#list .srow:not(.nav, .queued)");
    const clear = (r) => {
      for (const k of ["cleanK", "cleanTile", "cleanFace"]) delete r.dataset[k];
      r.style.removeProperty("--clean-face");
      const txt = r.querySelector(":scope > .txt");
      if (txt) delete txt.dataset.cleanLetter;
    };
    if (!document.documentElement.classList.contains("th-clean-av")) { for (const r of document.querySelectorAll("#list .srow[data-clean-k]")) clear(r); return; }
    const ix = sessions(), dirs = hub.snapshot?.()?.directions || [];
    const firstRow = document.querySelector("#list .sgroup.bare .srow");
    for (const r of rows) {
      const s = rowSession(r, ix, firstRow);
      if (!s) { if (r.dataset.cleanK) clear(r); continue; }
      const hubRow = s.kind === "hub";
      const k = hubRow ? "hub" : s.role === "agent" ? "agent" : dirs.includes(s.name) || s.backlog ? "dir" : r.closest("li.forks") ? "exec" : "other";
      put(r, "cleanK", k);
      put(r, "cleanTile", k === "dir" ? "1" : "");
      const u = k === "dir" ? "" : hubRow ? hubCharacter(s) : characterOf(s, ix.byName), v = u ? `url("${u}")` : "";
      put(r, "cleanFace", hubRow && !s.icon && !u ? "1" : "");   // no head of its own to draw: the hub's icon (CSS)
      if (v) { if (r.style.getPropertyValue("--clean-face") !== v) r.style.setProperty("--clean-face", v); }
      else if (r.style.getPropertyValue("--clean-face")) r.style.removeProperty("--clean-face");
      const txt = r.querySelector(":scope > .txt");
      if (txt) put(txt, "cleanLetter", k === "dir" || hubRow || u || r.querySelector(".nm > .avatar") ? "" : Array.from(s.name)[0].toUpperCase());
    }
  };
  // The core orders a group through hub.addOrder, but the owner's manual order (a drag in a group writes the whole group order) is applied
  // after plugins, and the "Sleeping" group is not ordered by plugins at all. Those lists get the same order here: each top-level row
  // with its executors (li.forks) travels as one block. Same order already — nothing is moved (no churn on every snapshot); the
  // "Now" sections (by state) and groups of a plugin (sidebar-groups, subgroups) are left alone
  const recentOrder = () => {
    const l = document.getElementById("list");
    if (!l || !recent() || l.querySelector(":scope > section.asec")) return;
    const byName = sessions().byName;
    for (const ul of l.querySelectorAll(":scope > section.sgroup:not(.sg-own) > ul, :scope > details.resting > ul")) {
      const blocks = [];
      for (const li of ul.children) {
        if (li.matches("li.forks")) { blocks.at(-1)?.push(li); continue; }
        if (!li.dataset.s && !li.dataset.m) { blocks.length = 0; break; }   // something we do not know (subgroup header, queue): not ours
        blocks.push([li]);
      }
      const rows = blocks.map((b) => byName.get(b[0].dataset.s || b[0].dataset.m));
      if (blocks.length < 2 || rows.includes(undefined)) continue;
      const want = byRecent(rows);
      if (want.every((r, i) => r === rows[i])) continue;
      ul.append(...want.flatMap((r) => blocks[rows.indexOf(r)]));
    }
  };
  // On every snapshot the core merges rows into the old nodes (reconcile.js); it keeps our attributes now, but a row put
  // in anew (another look, a rename of a group) comes without them. For a moment the detailed list is shorter (rows are single-line), and when it is scrolled to the bottom
  // the core immediately cuts the scroll to the new, smaller bottom (list.scrollTop = keep); once the rows are
  // two-line again the position does not come back: the list "throws you back" to the middle. So in the same
  // microtask, before painting, we put the attributes and the position back. The position comes from the scroll
  // event: a cut that has already happened cannot be read
  let listTop = 0;
  // A chat that rises to the top of its group pushes everything under it down, and a scrolled list would show other rows. So the
  // first few rows in view are remembered with their distance from the list top (on scroll and after every fix-up), and when the
  // rows have moved the list is scrolled back by the shift most of them share: the one that rose is the odd one out
  let seen = [], seenTop = 0, seenEnd = false;
  const lookAt = (l) => {
    seen = []; seenTop = l.scrollTop; seenEnd = l.scrollTop >= l.scrollHeight - l.clientHeight - 1;
    const box = l.getBoundingClientRect();
    for (const li of l.querySelectorAll("li[data-s], li[data-m], li[data-f]")) {
      const b = li.firstElementChild?.getBoundingClientRect?.();
      if (!b || !b.height || b.bottom <= box.top) continue;
      if (b.top >= box.bottom || seen.length >= 5) break;
      seen.push([rowKey(li), b.top - box.top]);
    }
  };
  const holdView = () => {
    const l = document.getElementById("list");
    if (!l || !recent()) { seen = []; return; }
    if (seen.length && l.scrollTop >= 1 && l.scrollTop === seenTop && !seenEnd) hold(l);   // at the top the new chat is simply seen, at the bottom the list stays there
    lookAt(l);
  };
  const hold = (l) => {
    const box = l.getBoundingClientRect(), shifts = new Map();
    for (const [key, top] of seen) {
      const li = key && l.querySelector(`li[data-s="${CSS.escape(key)}"], li[data-m="${CSS.escape(key)}"], li[data-f="${CSS.escape(key)}"]`);
      const b = li?.firstElementChild?.getBoundingClientRect?.();
      if (b) { const d = Math.round(b.top - box.top - top); shifts.set(d, (shifts.get(d) || 0) + 1); }
    }
    const best = [...shifts].sort((a, b) => b[1] - a[1] || Math.abs(a[0]) - Math.abs(b[0]))[0];
    if (best && best[0]) l.scrollTop += best[0];
  };
  const keepList = () => {
    const l = document.getElementById("list");
    if (!l || l.dataset.cleanKeepOn) return;
    l.dataset.cleanKeepOn = "1";
    l.addEventListener("scroll", () => { listTop = l.scrollTop; lookAt(l); }, { passive: true });
    // Sorting by the last message owns the order inside a group, so a row dropped in another place would just spring back and
    // look broken. The core's own drag yields to a plugin that took the press (preventDefault on pointerdown, after the core's
    // handler), so rows are not lifted while sorting is on; moving to another group stays in the row menu, group headers still drag
    l.addEventListener("pointerdown", (e) => { if (recent() && e.pointerType === "mouse" && e.target.closest("li[data-s] > .srow")) e.preventDefault(); });
    new MutationObserver(() => {
      if (!on()) return;
      recentOrder();
      rowFaces();   // a row put in anew comes without our avatar attributes too
      if (!document.documentElement.classList.contains("th-clean-detail")) return;
      if (l.scrollTop < l.scrollHeight - l.clientHeight - 1) return;   // not at the bottom: nothing to cut
      rowDetail();
      if (l.scrollTop < listTop) l.scrollTop = listTop;
    }).observe(l, { childList: true, subtree: true, attributes: true,
      attributeFilter: ["data-clean-d", "data-clean-k", "data-clean-tile", "data-clean-face", "data-clean-letter", "style"] });
  };
  // "Usage bars" (a "Look" option, off by default): the sidebar footer draws bars instead of the text line (usageDraw)
  // the tuned offsets of a reaction's emoji (clean.css) hold for Apple Color Emoji, the emoji font of macOS and iOS in every browser
  const APPLE_EMOJI = /mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform || "");
  const usageBars = () => on() && !!hub.option?.("usage");
  const detailClass = () => {   // the theme's options, only while it is chosen: other themes see no foreign classes on <html>
    document.documentElement.classList.toggle("th-clean-detail", on() && !!hub.option?.("detail"));
    document.documentElement.classList.toggle("th-clean-apple-emoji", on() && APPLE_EMOJI);
    document.documentElement.classList.toggle("th-clean-av", on() && !!hub.option?.("avatars"));
    document.documentElement.classList.toggle("th-clean-recent", on() && !!hub.option?.("recent"));
    document.documentElement.classList.toggle("th-clean-counts", on() && !!hub.option?.("counts"));
    document.documentElement.classList.toggle("th-clean-usage", usageBars());
    document.documentElement.classList.toggle("th-clean-footcounts", on() && !!hub.option?.("footcounts"));
    document.documentElement.classList.toggle("th-clean-narrow", on() && !!hub.option?.("narrow"));
    document.documentElement.classList.toggle("th-clean-waitbox", on() && !!hub.option?.("waitbox"));
  };
  detailClass();
  addEventListener("hub-redraw", () => { detailClass(); sweep(); });
  setInterval(() => { if (on()) rowDetail(); }, 20000);   // minutes pass even without a core redraw
  // the edits first, then what reads the layout (the scroll of the list and the feed, the rows in view): a read after an
  // edit makes the browser lay the page out, so one pass lays it out once, not after every edit
  // the "Schedule" tag in the ☰ menu (paused / expired) is one .tag node with no class for its state: which one it is shows only
  // in the core's words; clean.css paints "paused" soft yellow and "expired" gray
  const navTags = () => {
    const ended = T("web.schedule.expired_tag", "");
    for (const g of document.querySelectorAll("#nav-extra .srow.nav > .tag")) {
      const k = ended && g.textContent === ended ? "ended" : "paused";
      if (g.dataset.cleanTag !== k) g.dataset.cleanTag = k;
    }
  };
  const sweep = () => {
    if (!on()) return;
    navTags(); tagBylines(document); splitTabs(); tabMarks(); rowTitles(); waitCounts(); chevrons(); tabsFold(); maskIcons(); searchBox(); foldButtons();
    avatars(); boardCount(); headNote(); groupTools(); placeholders(); separators(); forkCounts(); recBars(); actions(); queuePlate(); notices(); finishItem();
    boardRecap(); rowDetail(); rowFaces(); recentOrder(); keepList();
    boardHint(); fade(); feedEnd(); holdView(); foldTop();
    headFold();   // reads computed style (is a switch hidden by the narrow header?) — after the layout the reads above already paid for
    fit.hook();
  };
  loadTags(() => { if (on()) sweep(); });   // faces.json: the heads of the specialists by their role (faces.js)

  // The feed changes often — no more than once a frame. The core's stopwatch rewrites its text every second (main.js): a batch
  // of only such text gets only what reads that text — the " · " mark and the row tooltip, — not the whole sweep (it read the
  // scroll of the list and laid the page out every second while nothing happened). The sweep's own edits do not wake it again
  const TICKS = ".elapsed, .step-run, [data-next], .ago";
  const tick = (m) => !!(m.target.nodeType === 1 ? m.target : m.target.parentElement)?.closest(TICKS);
  let queued = false, whole = false;
  const watch = new MutationObserver((list) => {
    if (!list.every(tick)) whole = true;
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      if (whole) { whole = false; sweep(); } else if (on()) { separators(); rowTitles(); }
      watch.takeRecords();
    });
  });
  watch.observe(document.body, { childList: true, subtree: true, characterData: true });

  // limits at the bottom of the sidebar (the sidebar.footer place): by default the text line "work  5 h 10%  week 1%" (limitsLine);
  // with "Usage bars" on — the window bars as in the native app (usageDraw, usage.js)
  // limits — at the bottom of the sidebar (the sidebar.footer place), from the core snapshot: "work  5 h 10%  week 1%"; share and reset —
  // in the tooltip. A window without a name (the gateway sends new ones, e.g. seven_day_overage_included) stays out of the line —
  // a raw key instead of a label is worse than nothing
  const SHORT = { five_hour: ["web.limits.short_five_hour", "5 h"], seven_day: ["web.limits.short_week", "week"],
    seven_day_opus: ["web.limits.short_week_opus", "Opus"], seven_day_sonnet: ["web.limits.short_week_sonnet", "Sonnet"] };
  const FULL = { five_hour: ["web.limits.name_five_hour", "5 hours"], seven_day: ["web.limits.name_week", "Week"],
    seven_day_opus: ["web.limits.name_week_opus", "Week · Opus"], seven_day_sonnet: ["web.limits.name_week_sonnet", "Week · Sonnet"] };
  const limitsLine = (root, snap) => {
    const wins = on() ? Object.entries(snap?.limits?.windows || {}).filter(([name, w]) => name in SHORT && typeof w.used === "number") : [];
    if (!wins.length) { root.replaceChildren(); return; }
    const order = Object.keys(SHORT);
    wins.sort(([a], [b]) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
    const line = hub.el("span", "clean-lim");
    const acc = snap?.accounts?.on ? snap?.account?.active : null;
    if (acc) line.append(hub.el("span", "limseg limacc", acc));
    const now = Date.now() / 1000;
    // percentage color — as LimitsBlock.tone in the native UI: economy — yellow (it has no red and orange "running out"),
    // otherwise 95%+ and "not working" (limits.status = rejected) — red, 80–95% — orange, the rest — gray
    const stopped = snap?.limits?.status === "rejected", eco = snap?.budget?.mode === "economy";
    const tone = (u) => eco ? "eco" : stopped || u >= 95 ? "high" : u >= 80 ? "warn" : "ok";
    // Codex counts down, as its /status: the line says "47%", the tooltip "47% left" (the colour still follows the share used)
    const down = countsDown(snap?.limits), left = T("web.limits.left", USAGE_WORDS.left);
    for (const [name, w] of wins) {
      const used = w.resets && w.resets < now ? 0 : Math.round(w.used * 100);
      const seg = hub.el("span", "limseg");
      seg.dataset.tone = tone(used);
      const reset = w.resets && w.resets > now ? clock(w.resets, hub.locale, { day: "numeric", month: "short" }) : "";
      seg.dataset.full = `${T(...FULL[name])} ${pctText(used, down, left)}`;
      seg.title = seg.dataset.full + (reset ? ` · ${reset}` : "");   // short names in the line; the full name and reset — in the tooltip
      const [k, fb] = SHORT[name];
      seg.append(hub.el("span", "limname", T(k, fb)), hub.el("span", "limpct", pctNum(used, down)));
      line.append(seg);
    }
    line.title = [...line.querySelectorAll(".limseg[data-full]")].map((x) => x.dataset.full).join(" · ");
    if (acc) {   // accounts are on: a click shows the limits of all accounts
      line.classList.add("limlink");
      line.tabIndex = 0;
      line.setAttribute("role", "link");
      const open = () => hub.openSettings?.("accounts");
      line.addEventListener("click", open);
      line.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
    }
    root.replaceChildren(line);
  };

  // The usage bars — as LimitsBlock in the native app; data — usage.js.
  // Compact: two bars (5 hours 28%, week 72%), the accounts button (accounts on) and the switch; the percent pops up over a bar
  // on hover or tap and moves nothing. Expanded: a row per window (name, "resets in …" or "N% ⏱" for an old reading) over its
  // bar — the weekly Opus and Sonnet rows too, when the core sends them — then the e-mail with ✓ and the switch.
  // The choice is kept in localStorage; without one — compact on a desktop (the block is always on screen), expanded on a phone.
  // The block is built again only when what it shows changes; hover, tap and the old reading's hint only show and hide nodes
  // that are already there, so the node under the pointer is never replaced and a hint cannot stay behind.
  const USAGE_KEY = "clean.usage";
  const usageOpen = () => {
    let v = usage.choice;   // this page's choice first: a refused setItem must not undo the switch
    if (v == null) try { v = localStorage.getItem(USAGE_KEY); } catch { /* storage off */ }
    return v ? v === "open" : matchMedia("(max-width: 760px)").matches;
  };
  const usage = { choice: null, key: "", root: null, box: null, m: null, snap: null, at: 0, peek: null, mark: null, hold: false, tip: null,
    timer: 0, drawing: false, ro: null };
  // the words of the block from the catalogs; usage.js fills {when} and {time}
  const usageWords = () => ({
    five_hour: T("web.limits.name_five_hour", USAGE_WORDS.five_hour), seven_day: T("web.limits.name_week", USAGE_WORDS.seven_day),
    seven_day_opus: T("web.limits.name_week_opus", USAGE_WORDS.seven_day_opus), seven_day_sonnet: T("web.limits.name_week_sonnet", USAGE_WORDS.seven_day_sonnet),
    mark: T("web.clean.usage_mark", USAGE_WORDS.mark), lapsed: T("web.clean.usage_lapsed", USAGE_WORDS.lapsed),
    compact: T("web.clean.usage_compact", USAGE_WORDS.compact), expanded: T("web.clean.usage_expanded", USAGE_WORDS.expanded),
    accounts: T("web.settings.tab.accounts", USAGE_WORDS.accounts), active: T("web.clean.usage_active", USAGE_WORDS.active),
    soon: T("web.clean.usage_soon", USAGE_WORDS.soon), left: T("web.limits.left", USAGE_WORDS.left), resets: T("web.clean.usage_resets", USAGE_WORDS.resets, { when: "{when}" }),
    at: T("web.clean.usage_at", USAGE_WORDS.at, { time: "{time}" }), link: T("web.clean.usage_link", USAGE_WORDS.link, { time: "{time}" }),
    cache: T("web.clean.usage_cache", USAGE_WORDS.cache), idle: T("web.clean.usage_idle", USAGE_WORDS.idle), rest: T("web.clean.usage_rest", USAGE_WORDS.rest),
  });
  const rtl = () => document.documentElement.dir === "rtl";
  // caption x over a bar, from its start edge: the percent — over the middle of the filled part, the even-pace note — over
  // the mark; never closer to the edge than half the caption (as outlookX in the native app)
  const captionAt = (tip, w, x) => {
    const half = Math.max(tip.offsetWidth / 2, 1);
    tip.style.insetInlineStart = `${Math.min(Math.max(x, half), Math.max(half, w - half))}px`;
  };
  const captionText = (m, w) => w.lapsed ? m.words.lapsed : usage.mark === w.id ? m.words.mark : w.text;
  // applies hover, tap, keyboard and the hint of an old reading to the nodes that are already on the page
  function usageShow() {
    const { box, m } = usage;
    if (!box || !m) return;
    for (const host of box.querySelectorAll(".cu-bar, .cu-row")) {
      const w = m.windows.find((x) => x.id === host.dataset.id);
      const tip = host.querySelector(".cu-tip, .cu-peek");
      if (!w || !tip) continue;
      const shown = usage.peek === w.id && (host.classList.contains("cu-bar") || (!m.stale && !w.lapsed));
      tip.hidden = !shown;
      if (!shown) continue;
      tip.textContent = host.classList.contains("cu-row") ? ` · ${captionText(m, w)}` : captionText(m, w);
      if (!host.classList.contains("cu-bar")) continue;
      // placed after layout: over the filled part or over the mark, measured from the bar's start edge
      const r = host.querySelector(".cu-track").getBoundingClientRect(), base = tip.offsetParent?.getBoundingClientRect() || r;
      const x = usage.mark === w.id && w.even != null ? r.width * w.even : w.lapsed ? r.width / 2 : w.down ? r.width * (1 + w.used) / 2 : r.width * w.used / 2;
      captionAt(tip, base.width, (rtl() ? base.right - r.right : r.left - base.left) + x);
    }
    for (const b of box.querySelectorAll(".cu-clock")) {
      const shown = usage.tip === b.dataset.id;
      b.setAttribute("aria-expanded", String(shown));
      b.querySelector(".cu-hint").hidden = !shown;
    }
  }
  const peekShow = (id, mark = false, hold = 0) => {
    clearTimeout(usage.timer);
    usage.peek = id; usage.mark = mark ? id : null; usage.hold = !!hold;
    if (hold) usage.timer = setTimeout(() => { if (usage.peek === id) { usage.peek = usage.mark = null; usage.hold = false; usageShow(); } }, hold);
    usageShow();
  };
  const peekHide = (id) => { if (usage.peek === id) { clearTimeout(usage.timer); usage.peek = usage.mark = null; usage.hold = false; usageShow(); } };
  const tipSet = (id, on) => {
    const v = on ? id : usage.tip === id ? null : usage.tip;
    if (usage.tip !== v) { usage.tip = v; usageShow(); }
  };

  // one bar: track, fill, even-pace mark
  const usageBar = (w, old) => {
    const track = hub.el("span", "cu-track");
    if (!w.lapsed) {
      const fill = hub.el("span", "cu-fill");
      fill.style.width = `${w.used * 100}%`;
      if (old) fill.classList.add("old");
      track.append(fill);
    }
    if (w.even != null) {
      const mark = hub.el("span", "cu-mark");
      mark.style.insetInlineStart = `${w.even * 100}%`;
      track.append(mark);
    }
    return track;
  };
  // hover and tap on a bar or a row: the mark is a target of its own (± 6 px, ± 10 for a finger)
  const wirePeek = (node, w, peekable) => {
    const nearMark = (e, slack) => {
      if (w.even == null) return false;
      const r = node.querySelector(".cu-track").getBoundingClientRect();
      const x = rtl() ? r.right - r.width * w.even : r.left + r.width * w.even;
      return Math.abs(e.clientX - x) <= slack;
    };
    node.addEventListener("pointermove", (e) => {
      if (e.pointerType !== "mouse" || !peekable) return;
      const m = nearMark(e, 6);
      if (usage.peek !== w.id || !!usage.mark !== m) peekShow(w.id, m);
    });
    node.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") peekHide(w.id); });
    node.addEventListener("click", (e) => {
      if (!peekable && !nearMark(e, 10)) return;
      // a click from the keyboard (Enter, Space) keeps the percent while the focus stays; a tap hides it after 3 s
      peekShow(w.id, nearMark(e, e.pointerType === "mouse" ? 6 : 10), e.detail === 0 ? 0 : 3000);
    });
    // a rebuild puts the focus back on the new button: that focus (and a blur of the old one) is not the user's — without
    // the check Tab onto a bar rebuilt the block in a loop until the stack overflowed. The hide on blur waits until the focus
    // has moved: a rebuild in the middle of Tab replaced the button the focus was going to, and it fell out of the block
    node.addEventListener("focus", () => { if (!usage.drawing && peekable && usage.peek !== w.id && node.matches(":focus-visible")) peekShow(w.id); });
    node.addEventListener("blur", () => { if (!usage.drawing) setTimeout(() => peekHide(w.id)); });
  };
  // clock next to an old reading: the hint says when it was measured and why it is old; usage.tip holds the id of the
  // window whose hint is open, so only that hint shows
  const usageClock = (stale, id) => {
    const b = hub.el("button", "cu-clock");
    b.type = "button";
    b.dataset.id = id;
    b.setAttribute("aria-label", stale.text.replace("\n", ". "));
    b.setAttribute("aria-expanded", "false");
    const tip = hub.el("span", "cu-hint", stale.text);
    tip.hidden = true;
    tip.setAttribute("role", "tooltip");
    b.append(tip);
    b.addEventListener("click", (e) => { e.stopPropagation(); tipSet(id, usage.tip !== id); });
    b.addEventListener("pointerenter", (e) => { if (e.pointerType === "mouse") tipSet(id, true); });
    b.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") tipSet(id, false); });
    b.addEventListener("keydown", (e) => { if (e.key === "Escape") tipSet(id, false); });
    b.addEventListener("blur", () => { if (!usage.drawing) setTimeout(() => tipSet(id, false)); });
    return b;
  };
  const usageSwitch = (open, m) => {
    const g = hub.el("span", "cu-switch");
    g.setAttribute("role", "group");
    for (const [icon, expanded] of [["bars", false], ["list", true]]) {
      const b = hub.el("button", `cu-seg cu-seg-${icon}`);
      b.type = "button";
      b.setAttribute("aria-label", expanded ? m.words.expanded : m.words.compact);
      b.setAttribute("aria-pressed", String(open === expanded));
      b.addEventListener("click", () => {
        if (open === expanded) return;
        try { localStorage.setItem(USAGE_KEY, expanded ? "open" : "compact"); } catch { /* storage off: this page only */ }
        usage.choice = expanded ? "open" : "compact";
        usage.peek = usage.mark = usage.tip = null; usage.hold = false;
        usageDraw();
        usage.root?.querySelector(`.cu-seg-${icon}`)?.focus();
      });
      g.append(b);
    }
    return g;
  };
  // the account line: whole e-mail; if it does not fit — without the top-level zone; then CSS cuts the end with "…"
  const fitMail = (span, steps) => {
    const run = () => {
      if (!span.isConnected) return;
      for (const s of steps) { span.textContent = s; if (span.scrollWidth <= span.clientWidth) return; }
    };
    requestAnimationFrame(run);
    if (typeof ResizeObserver === "function") {
      usage.ro?.disconnect();
      usage.ro = new ResizeObserver(run);
      usage.ro.observe(span);
    }
  };
  // the account line of the expanded view: its own row under the windows, as wide as they are — next to the menu
  // button and the switch an e-mail had under 100 px and was cut inside the domain; with accounts on it opens their settings
  const usageAcc = (m) => {
    const acc = hub.el(m.pool ? "button" : "span", "cu-acc");
    if (m.pool) { acc.type = "button"; acc.addEventListener("click", () => hub.openSettings?.("accounts")); }
    const shown = hub.el("span", "cu-mail", m.line);
    shown.setAttribute("aria-hidden", "true");
    shown.title = m.line;
    acc.append(shown, hub.el("span", "cu-sr", m.line));
    fitMail(shown, m.email ? emailSteps(m.email) : [m.line]);
    if (m.active) {
      const ok = hub.el("span", "cu-active");
      ok.setAttribute("role", "img");
      ok.setAttribute("aria-label", m.words.active);
      acc.append(ok);
    }
    return acc;
  };
  const usageFoot = (m, open) => {
    const foot = hub.el("div", `cu-foot${open ? " open" : ""}`);
    if (!open) {
      const bars = hub.el("div", "cu-bars");
      const main = m.windows.filter((w) => !w.extra);
      // the compact view has two bars: the weekly Opus and Sonnet windows are in the week bar's hint (the last bar when there is no week)
      const more = m.windows.filter((w) => w.extra);
      const hint = main.find((w) => w.id === "seven_day") || main[main.length - 1];
      for (const w of main) {
        const b = hub.el("button", "cu-bar");
        b.type = "button";
        b.dataset.tone = w.tone;
        b.dataset.id = w.id;
        b.style.flexGrow = main.length === 1 ? "1" : w.id === "five_hour" ? "28" : "72";
        const said = [w, ...(w === hint ? more : [])].map((x) => x.spoken);
        b.setAttribute("aria-label", m.stale ? `${said.join(". ")}. ${m.stale.text.replace("\n", ". ")}` : said.join(". "));
        if (said.length > 1) b.title = said.join("\n");
        const tip = hub.el("span", "cu-tip");
        tip.hidden = true;
        tip.setAttribute("aria-hidden", "true");
        b.append(usageBar(w, !!m.stale && !m.broken), tip);
        wirePeek(b, w, true);
        bars.append(b);
      }
      foot.append(bars);
      if (m.pool) {   // accounts are on: a button to their settings (the usage of every account)
        const a = hub.el("button", "cu-accbtn");
        a.type = "button";
        a.setAttribute("aria-label", m.words.accounts);
        a.title = m.words.accounts;
        a.addEventListener("click", () => hub.openSettings?.("accounts"));
        foot.append(a);
      }
    }
    foot.append(usageSwitch(open, m));
    return foot;
  };
  const usageRows = (m) => {
    const rows = hub.el("div", "cu-rows");
    const old = !!m.stale;
    for (const w of m.windows) {
      const row = hub.el("button", "cu-row");
      row.type = "button";
      row.dataset.tone = w.tone;
      row.dataset.id = w.id;
      if (old) row.classList.add("old");
      row.setAttribute("aria-label", old ? `${w.spoken}. ${m.stale.text.replace("\n", ". ")}` : w.spoken);
      const line = hub.el("span", "cu-line");
      line.setAttribute("aria-hidden", "true");
      // the name stays at the start and the reset at the end in every state; the percent (or "even pace") joins the name
      const name = hub.el("span", "cu-name", w.name);
      const peek = hub.el("span", "cu-peek");
      peek.hidden = true;
      name.append(peek);
      line.append(name);
      if (w.lapsed) line.append(hub.el("span", "cu-val", m.words.lapsed));
      else if (old) line.append(hub.el("span", "cu-val cu-pct", w.text));
      else if (w.reset) line.append(hub.el("span", "cu-val", w.reset));
      row.append(line, usageBar(w, old && !m.broken));
      wirePeek(row, w, !old);
      const wrap = hub.el("div", "cu-rowwrap");
      wrap.append(row);
      if (old && !w.lapsed) wrap.append(usageClock(m.stale, w.id));
      rows.append(wrap);
    }
    return rows;
  };
  function usageDraw() {
    const root = usage.root;
    if (!root) return;
    const now = Date.now() / 1000;
    const linkDown = !!document.getElementById("connection-side")?.classList.contains("on");
    const m = on() ? usageModel(usage.snap, { now, words: usageWords(), locale: hub.locale, linkDown, sinceSnap: usage.at ? (Date.now() - usage.at) / 1000 : null }) : null;
    const open = usageOpen();
    // the slot renders on every snapshot and patch: an unchanged block is not rebuilt, so the focus and a screen reader stay
    // put (the even mark is compared to half a percent)
    const key = m ? JSON.stringify([m, m.windows.map((w) => w.even == null ? null : Math.round(w.even * 200)), open, rtl()],
      (k, v) => (k === "even" ? undefined : v)) : "";
    if (key === usage.key && root.firstChild) return;
    usage.key = key;
    usage.ro?.disconnect();
    usage.m = m;
    if (!m) { usage.box = null; root.replaceChildren(); return; }
    const box = hub.el("div", `clean-usage${open ? " open" : ""}`);
    if (m.broken) box.dataset.state = "broken";
    if (open) box.append(usageRows(m));
    if (open && m.line) box.append(usageAcc(m));
    box.append(usageFoot(m, open));
    // the focus goes back to the same control (same classes and window), not to the same index: buttons come and go
    const was = root.contains(document.activeElement) ? document.activeElement : null;
    usage.drawing = true;
    try {
      root.replaceChildren(box);
      usage.box = box;
      if (was) [...root.querySelectorAll("button")].find((b) => b.className === was.className && b.dataset.id === was.dataset.id)
        ?.focus({ preventScroll: true });
    } finally { usage.drawing = false; }
    usageShow();
    // the pointer may have left while the old nodes were being replaced, and a replaced node reports no leave: a hover that
    // the new nodes do not confirm is dropped (a tap's 3 s and a focus on the node stay)
    if (usage.peek && !usage.hold) setTimeout(() => {
      const host = usage.box?.querySelector(`[data-id="${usage.peek}"]:is(.cu-bar, .cu-row)`);
      if (usage.peek && !usage.hold && !host?.matches(":hover, :focus")) { usage.peek = usage.mark = null; usageShow(); }
    }, 300);
    if (usage.tip != null) setTimeout(() => {
      const host = usage.box?.querySelector(`.cu-clock[data-id="${usage.tip}"]`);
      if (usage.tip != null && !host?.matches(":hover, :focus")) { usage.tip = null; usageShow(); }
    }, 300);
  }
  setInterval(() => { if (usage.root?.isConnected) usageDraw(); }, 60000);   // "resets in …", the even mark, the reading's age
  hub.addSlot("sidebar.footer", {
    id: "clean-limits",
    render(root, snap) {
      if (!usageBars()) { usage.ro?.disconnect(); usage.root = null; usage.box = null; usage.key = ""; limitsLine(root, snap); return; }
      if (snap !== usage.snap) { usage.snap = snap; usage.at = snap ? Date.now() : 0; }
      usage.root = root;
      usageDraw();
    },
  });

  // session icon: the core puts the popup on the top-left corner of the clicked avatar (a DOMRect has x and y) — and covers it.
  // The theme moves the popup right after it appears (before the frame): below the avatar; if it does not fit — above, then right and left.
  // On a phone the window is the core's bottom sheet; nothing is moved there
  let anchor = null;
  addEventListener("pointerdown", () => { anchor = null; }, true);
  // ☰ opened with a mouse or a tap: the core focuses "Settings" from a script, which a browser may count as keyboard focus — so the
  // menu is marked and clean.css drops the ring; the first key pressed in the menu or on ☰ clears the mark, the keyboard ring is back
  const navPtr = (e) => {
    const box = document.getElementById("nav-extra");
    if (!box) return;
    if (e.type === "pointerdown" && on() && e.target.closest?.("#navmenu, #nav-extra")) box.dataset.cleanPtr = "";
    else if ("cleanPtr" in box.dataset) delete box.dataset.cleanPtr;
  };
  document.addEventListener("pointerdown", navPtr, true);
  document.addEventListener("keydown", navPtr, true);
  addEventListener("click", (e) => { anchor = e.target.closest?.(".avatar, .head-av") || null; }, true);
  const placePalette = (pop) => {
    if (!on() || !anchor?.isConnected || matchMedia("(max-width: 760px)").matches) return;
    const a = anchor.getBoundingClientRect(), r = pop.getBoundingClientRect(), G = 8, W = innerWidth, H = innerHeight;
    const across = (v) => Math.max(G, Math.min(v, W - r.width - G)), along = (v) => Math.max(G, Math.min(v, H - r.height - G));
    const spots = [
      [a.bottom + G + r.height <= H - G, across(a.left), a.bottom + G],
      [a.top - G - r.height >= G, across(a.left), a.top - G - r.height],
      [a.right + G + r.width <= W - G, a.right + G, along(a.top)],
      [a.left - G - r.width >= G, a.left - G - r.width, along(a.top)],
    ];
    const [, x, y] = spots.find((s) => s[0]) || spots[0];
    pop.style.left = `${x}px`;
    pop.style.top = `${y}px`;
  };
  new MutationObserver((list) => {
    for (const m of list) for (const n of m.addedNodes) if (n.classList?.contains("palette") && n.classList.contains("gpop")) placePalette(n);
  }).observe(document.body, { childList: true });

  // the core's theme-color follows the system scheme (prefers-color-scheme), but Clean is light or dark on its own: on a phone the
  // status bar and the address bar would be painted a foreign color. While the theme is selected, both tags get the shell color
  // (--c-shell in clean.css: the background under the windows; it cannot be read from the style — on the first run the stylesheet
  // is not loaded yet), when it is switched off — their own previous values
  let tcSaved = null;
  const themeColor = () => {
    const metas = [...document.querySelectorAll('meta[name="theme-color"]')];
    if (!metas.length) return;
    if (on()) {
      tcSaved ??= metas.map((m) => m.content);
      for (const m of metas) m.content = document.documentElement.classList.contains("th-clean-dark") ? "#121214" : "#f1f1f3";
    } else if (tcSaved) {
      metas.forEach((m, i) => { if (tcSaved[i]) m.content = tcSaved[i]; });
      tcSaved = null;
    }
  };

  // Voice recording from the keyboard. The core (recorder.js) cancels the recording on a single Esc and does nothing on Enter; in the theme:
  // - Enter while recording — stop and send: the core's stop button is pressed (the plugin has no recording method);
  // - Esc twice within VOICE_ESC_MS — cancel (the second Esc is passed to the core: it cancels, like "Cancel");
  // - a single Esc leaves the recording alone: the window eats it in the capture phase before the core (which catches Esc on document).
  // "Recording" is read from the core's record strip, by class, never by button text: .recstrip holds the clock
  // [data-rec-clock] while recording and the round cancel button .rec-cancel while recording or opening the mic; while the
  // core recognises speech the strip has neither, so keys pass through. There is one permanent listener, nothing to arm or
  // disarm: outside recording and outside the theme it does nothing.
  // Enter leaves other fields alone (search, dialogs, buttons); Esc works anywhere — as in the core.
  const VOICE_ESC_MS = 400, SPENT_MS = 800;
  let lastEsc = 0, spentAt = 0;   // spentAt: when Enter or a double Esc fired — the strip may not have changed yet
  const recStrip = () => document.querySelector("form.composer.rec .recstrip");
  const fieldFocused = (a) => {
    if (!a || a === document.body) return false;
    if (a.closest?.("dialog[open], [role=dialog], [role=menu], [role=listbox]")) return true;
    if (a.matches?.("input, textarea, select, button, a, summary, [role=button], [contenteditable]:not([contenteditable=false])")) {
      return !a.matches?.("form.composer textarea, form.composer .btn.stop");   // the conversation's input field and the recording stop are ours
    }
    return false;
  };
  addEventListener("keydown", (e) => {
    if (!on() || (e.key !== "Escape" && e.key !== "Enter")) return;
    if (e.key === "Escape" && tabsOpen) { e.preventDefault(); e.stopImmediatePropagation(); tabsClose(true); return; }   // Settings sections list
    const strip = recStrip(), clock = strip?.querySelector("[data-rec-clock]");
    if (!strip || !strip.querySelector("[data-rec-clock], .rec-cancel")) { lastEsc = 0; return; }
    const spent = e.timeStamp - spentAt < SPENT_MS;
    if (e.key === "Escape") {
      if (spent) { e.stopImmediatePropagation(); return; }   // the cancel has already gone out, the strip has not changed yet
      if (e.repeat) { e.stopImmediatePropagation(); return; }   // holding the key is not a second press
      if (lastEsc && e.timeStamp - lastEsc <= VOICE_ESC_MS) { lastEsc = 0; spentAt = e.timeStamp; return; }   // the core does the cancel
      lastEsc = e.timeStamp;
      e.stopImmediatePropagation();
      return;
    }
    if (!clock || spent || e.repeat || e.isComposing || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey || fieldFocused(document.activeElement)) return;
    const stop = document.querySelector("form.composer.rec .bar > .btn.round.stop");
    if (!stop) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    spentAt = e.timeStamp;
    stop.click();
  }, true);

  // Tall shots (a phone screenshot, ~390x2400): the core fits any image into the window by height, so they shrink to a
  // strip. The lightbox is the core's node, so only a class is set on it (the core never writes it): CSS lays the image
  // out at screen width (never above its natural size) with vertical scrolling. Idempotent: re-evaluated on every
  // image load, so paging resets it.
  const TALL_RATIO = 2;
  const tallMark = (e) => {
    const i = e.target;
    if (!on() || !i?.matches?.(".lb-img")) return;
    const box = i.closest(".lightbox");
    if (!box) return;
    const tall = e.type === "load" && i.naturalWidth > 0 && i.naturalHeight / i.naturalWidth > TALL_RATIO;
    box.classList.toggle("clean-tall", tall);
    box.scrollTop = 0;
    // keys scroll the focused element's scroller: the core focuses ✕, which is position: fixed here, so its scroller is
    // the page (overflow hidden under an open lightbox) and PageDown / ↓ / End do nothing — focus the lightbox itself
    if (tall) { box.tabIndex = -1; box.focus?.({ preventScroll: true }); }
  };
  // on document, not window: a load event never reaches window listeners (the DOM spec drops Window from its path)
  document.addEventListener("load", tallMark, true);
  document.addEventListener("error", tallMark, true);

  // the theme was chosen or changed (js/themes.js sets the class, then sends hub-theme): switched off — what the theme changed
  // in the page goes back
  let active = false;
  const sync = () => {
    detailClass();
    if (active !== on()) {
      active = on();
      for (const b of document.querySelectorAll(".byline[data-clean]")) { delete b.dataset.clean; delete b.dataset.cleanHub; }
      if (!on()) {
        for (const b of document.querySelectorAll(".lightbox.clean-tall")) b.classList.remove("clean-tall");
        for (const x of document.querySelectorAll("[data-settings-tab] .clean-att")) x.replaceWith(" ·");
        unmaskIcons(); unAvatars(); fade(); feedEnd(); unPlaceholders(); unPlate(); fit.reset();
        for (const h of document.querySelectorAll(".chat-head[data-clean-label]")) { h.removeAttribute("aria-label"); h.removeAttribute("title"); delete h.dataset.cleanLabel; }
        tabsOff();
        for (const x of document.querySelectorAll(".search-row .clean-x, .clean-bg, .clean-fold, .clean-fold-show, .clean-bars, .ce-n, .clean-recap")) x.remove();
      }
      hub.redraw?.();
    }
    themeColor();
    sweep();
  };
  addEventListener("hub-theme", sync);
  sync();
}
