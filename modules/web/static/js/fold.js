// Folded groups of the session list — one rule for all headers: the owner's groups, the "Now" sections and plugin
// subgroups (hub.addSubgroups, 1.31; a subgroup id is "<group>/<subgroup>"). The browser remembers the owner's choice:
// folded ones in hub.closedGroups (as before), opened ones in hub.openGroups; with no choice made, the group's default
// applies (a plugin subgroup with open: false starts folded)
import { OLD_KEYS } from "./groups.js";

const read = (k) => { try { const v = JSON.parse(localStorage.getItem(k) || "[]"); return Array.isArray(v) ? v : []; } catch { return []; } };
// the old keys were titles of the standard groups — convert them once
const closed = new Set(read("hub.closedGroups").map((k) => OLD_KEYS[k] || k));
const opened = new Set(read("hub.openGroups"));
const shutFirst = new Set();   // folded by default — learned while drawing

export function foldDefault(id, open) { open === false ? shutFirst.add(id) : shutFirst.delete(id); }
export const isOpen = (id) => (closed.has(id) ? false : opened.has(id) || !shutFirst.has(id));
export function setOpen(id, open) {
  (open ? closed : opened).delete(id);
  (open ? opened : closed).add(id);
  try {
    localStorage.setItem("hub.closedGroups", JSON.stringify([...closed]));
    localStorage.setItem("hub.openGroups", JSON.stringify([...opened]));
  } catch { /* storage unavailable — remembered until a reload */ }
}
