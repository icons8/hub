// The list on the left is rebuilt on every snapshot (~7 a minute even when idle). The browser does not deliver a click
// to a node removed from the page between mouse-down and mouse-up (symptom: a session row sometimes needs two clicks),
// so the new tree does not replace the old one wholesale but is merged into it: a row whose markup did not change stays
// the same node in the same place; a changed one is updated in place (attributes and content; the nodes inside the row
// stay the same too: the press lands on .nm or .lb inside a button, and a detached descendant drops the click just like a
// detached button); only departed rows are removed. Rows are matched by keys (session name, group, section) — the order
// of rows may change.
//
// The old node lives on with the handlers attached at build time. So "same markup — same node" holds only for the
// kinds listed below: their handlers read only what is visible in the markup (name, aria-expanded) and take the rest
// from the snapshot at click time. Everything else (buttons with their own data in a closure, badges) is replaced by
// the new node, as before. "Same markup" is the markup the node was put in with (__built), not the live one: a theme
// marks the nodes it shows (Clean rewrites the labels of .g-tools), and the live markup never matched again — the node was
// put in anew on every snapshot and the theme marked it again. Another look puts them in anew. A linked hub's row is a
// link (its menu is read from the row at the press, groups.js).
import { lookSig, unticked } from "./dom.js";

const KEEP = "li[data-s], li[data-m], li[data-f], li[data-q], li[data-sub], li[data-hub], .sgroup-h, .sgroup-hw, .fork-toggle, .g-tools, .rest-all";
let look = "";   // the look of the merge in progress (dom.js lookSig)

const keyOf = (n, prev) => {
  if (n.nodeType !== 1) return null;
  const d = n.dataset;
  if (n.matches("li")) return d.s ? "s:" + d.s : d.m ? "m:" + d.m : d.f ? "f:" + d.f : d.q ? "q:" + d.q : d.sub ? "sub:" + d.sub : d.hub ? "hub:" + d.hub : n.matches(".forks") ? "forks:" + prev : null;
  if (n.matches("section.sgroup")) return d.g ? "g:" + d.g : d.sec ? "sec:" + d.sec : "bare";
  if (n.matches("details.resting")) return "resting";
  if (n.matches("ul")) return "ul";
  if (n.matches(".srow")) return "srow";
  if (n.matches(".fork-toggle")) return "toggle";
  if (n.matches(".sgroup-h, .sgroup-hw")) return "head";
  if (n.matches(".g-tools")) return "tools";
  if (n.matches(".rest-all")) return "rest-all";
  return null;
};
const keys = (nodes) => {
  const m = new Map();
  let prev = null;
  for (const n of nodes) { const k = keyOf(n, prev); m.set(n, k); prev = k; }
  return m;
};
const MORPH = "section.sgroup, details.resting, ul, li[data-s], li[data-m], li[data-f], li[data-q], li[data-hub], li.forks";

// The attributes the core put on a node are remembered on it (__core: name → value). What else is on the node is a theme's
// or a plugin's mark (Clean: data-clean-*, its own tooltip and style) and stays: they used to be erased on every snapshot
// and put back by the theme — every row of the list was restyled each time. An attribute whose built value has not
// changed is left as it is on the page (the theme may have its own value there); a changed or dropped one is the core's.
// Without a theme's own attributes, if the set or order differs, all are removed and set in the new order: the markup in
// place then matches the freshly built one. A node the core has not merged yet in this look (put in whole, the look
// switched) gets the core's set as it is built: whatever else is there goes
const attrsOf = (n) => Object.fromEntries([...n.attributes].map((a) => [a.name, a.value]));
const stampOne = (n) => { n.__core = { look, attrs: attrsOf(n) }; };
const builtOf = (n) => look + "\0" + unticked(n.outerHTML);
// a node the merge puts in (or takes as it is): what the core put on it and inside it, and the built markup of the KEEP kinds
const own = (n, built = null) => {
  if (n.nodeType !== 1) return n;
  for (const c of [n, ...n.querySelectorAll("*")]) {
    stampOne(c);
    if (c.matches(KEEP)) c.__built = c === n && built ? built : builtOf(c);
  }
  return n;
};
function syncAttrs(to, from) {
  const was = to.__core?.look === look ? to.__core.attrs : null, now = attrsOf(from), live = attrsOf(to);
  to.__core = { look, attrs: now };
  const theirs = was ? Object.keys(live).filter((a) => !(a in now) && !(a in was)) : [];
  // on a node a theme marks, the value on the page stays where the core's has not changed since it put it (the theme's
  // tooltip, its style); not the classes — the core's own passing ones (a drag) are put right by the next build
  const val = (a) => (theirs.length && a !== "class" && a in was && was[a] === now[a] && a in live ? live[a] : now[a]);
  if (theirs.length) {   // a theme's own attributes are there: no reordering, only the core's changes
    for (const a of Object.keys(live)) if (a in was && !(a in now)) to.removeAttribute(a);
    for (const a of Object.keys(now)) if (live[a] !== val(a)) to.setAttribute(a, val(a));
    return;
  }
  if (Object.keys(live).join(" ") !== Object.keys(now).join(" ")) for (const a of Object.keys(live)) to.removeAttribute(a);
  for (const a of Object.keys(now)) if (to.getAttribute(a) !== val(a)) to.setAttribute(a, val(a));
}

// a plugin node on a row (ext.js hands out one per session) must not be taken out of the live row while a new one is
// built: a detached node drops a click on the badge just like a detached row. While the node is in the page, the build
// puts a marker (stand) in its place, and the row keeps the live node when merging; a marker left in a new row is
// replaced by the node in claim
export const stand = (root) => {
  if (!root.isConnected) return root;
  const m = document.createElement("span");
  m.dataset.plugin = root.dataset.plugin;
  m.standFor = root;
  return m;
};

// a node that looks the same can be updated in place: tag and class match (the .spin spinner is not a difference).
// Plugins (data-plugin) — ext.js hands out one node per session and the handlers are foreign: put it in as given, do not merge
const SPIN = / spin$/;
const cls = (n) => (n.getAttribute("class") || "").replace(SPIN, "");   // not className: on svg it is not a string
const alike = (o, n) => o.nodeType === n.nodeType && (o.nodeType !== 1 || (o.tagName === n.tagName
  && cls(o) === cls(n) && !n.hasAttribute("data-plugin")));

// row content: nodes in the same positions are updated (text via nodeValue, elements recursively), otherwise replaced
function morph(old, n) {
  syncAttrs(old, n);
  const want = [...n.childNodes];
  const live = new Set(want.map((w) => w.standFor).filter(Boolean));   // plugin nodes that will remain in the row
  want.forEach((w, i) => {
    let o = old.childNodes[i];
    if (w.standFor) {
      // plugin nodes are last in the row: whatever before it disappeared (a "waiting for you" badge, a dot) is removed,
      // while the node itself does not move; another plugin's node that is still needed is not touched (removing it
      // would detach it)
      while (o && o !== w.standFor && w.standFor.parentNode === old && !live.has(o)) { o.remove(); o = old.childNodes[i]; }
      if (o !== w.standFor) old.insertBefore(w.standFor, o || null);
    }
    else if (!o) old.append(own(w));
    else if (o.dataset?.plugin !== undefined) old.insertBefore(own(w), o);   // before a plugin node: it stays and shifts to the right
    else if (!alike(o, w)) old.replaceChild(own(w), o);
    else if (w.nodeType === 1) morph(o, w);
    else if (o.nodeValue !== w.nodeValue) o.nodeValue = w.nodeValue;
  });
  while (old.childNodes.length > want.length) old.lastChild.remove();
}

// what takes the place of old: old itself (updated in place) or the new node n
function settle(old, n) {
  const built = n.matches(KEEP) ? builtOf(n) : null;
  if (built && old.__built === built) return old;
  // a node put in without the merge (the list replaced whole for a rename) has no marks: its live markup, as before — and
  // from now on it is the merge's
  if (built && old.__built === undefined && builtOf(old) === built) return own(old, built);
  let to = old;
  if (n.matches(MORPH)) { syncAttrs(old, n); merge(old, [...n.childNodes]); }
  else if (n.matches(".srow")) morph(old, n);
  else to = own(n, built);
  if (built) to.__built = built;
  return to;
}

// which of the remaining nodes not to move: the longest chain increasing by previous position (pos is the previous
// position, or -1 for a new node). Only the rest are moved: a row that went down is moved alone, not all the rows it overtook
function still(pos) {
  const tails = [], prev = new Array(pos.length).fill(-1), keep = new Set();
  pos.forEach((p, i) => {
    if (p < 0) return;
    let lo = 0, hi = tails.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (pos[tails[mid]] < p) lo = mid + 1; else hi = mid; }
    if (lo) prev[i] = tails[lo - 1];
    tails[lo] = i;
  });
  for (let i = tails.length ? tails[tails.length - 1] : -1; i >= 0; i = prev[i]) keep.add(i);
  return keep;
}

function merge(parent, want) {
  const had = [...parent.childNodes], hadKey = keys(had), wantKey = keys(want), out = [], byKey = new Map();
  for (const o of had) {
    const k = hadKey.get(o);
    if (k == null) continue;
    if (!byKey.has(k)) byKey.set(k, []);
    byKey.get(k).push(o);
  }
  for (const n of want) {
    const k = wantKey.get(n), old = k == null ? null : byKey.get(k)?.shift();
    out.push(old ? settle(old, n) : own(n));
  }
  const stay = new Set(out);
  for (const o of had) if (!stay.has(o)) o.remove();   // departed and replaced ones first: the others are not shifted because of them
  const at = new Map([...parent.childNodes].map((n, i) => [n, i]));
  const keep = still(out.map((n) => at.get(n) ?? -1));
  // from the end: each node is placed before the next one that is already placed
  for (let i = out.length - 1, next = null; i >= 0; next = out[i--]) {
    if (!keep.has(i)) parent.insertBefore(out[i], next);
  }
}

// markers left in new rows (rows with no counterpart in the previous list) are replaced by the plugin nodes
export const claim = (list) => { for (const m of list.querySelectorAll("span[data-plugin]")) if (m.standFor) m.replaceWith(m.standFor); };

// list — the container; nodes — the freshly built children
export const mergeInto = (list, nodes) => { look = lookSig(); merge(list, nodes); claim(list); };
