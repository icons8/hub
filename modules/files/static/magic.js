// The file was rewritten before the reader's eyes (so a rewrite is noticeable: one thing deleted, one added,
// it is clear that magic is happening): the new version is already on screen, its text is compared with the old one by words:
// deleted text flashes red and collapses, added text fades in green, in turn, top to bottom. Then the clean view.
const SKIP = (n) => n.parentElement?.closest(".mk");  // code line numbers are not file text

export function textOf(root) {
  if (!root) return "";
  let s = "";
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, { acceptNode: (n) => (SKIP(n) ? 2 : 1) });
  while (w.nextNode()) s += w.currentNode.nodeValue;
  return s;
}

// a word together with the spaces after it: otherwise shared spaces cut a deleted phrase into separate words
const tokens = (s) => s.match(/^\s+|\S+\s*/g) || [];

// the word is the same if only the spaces after it differ (a line break between blocks and the like)
const same = (x, y) => x === y || (x.trimEnd() === y.trimEnd() && x.trimEnd() !== "");

// Word diff (LCS). Large files are compared by lines so as not to think for seconds.
function diff(a, b) {
  const n = a.length, m = b.length;
  if (n * m > 4e6) return null;
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    dp[i][j] = same(a[i], b[j]) ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const ops = [];
  let i = 0, j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && same(a[i], b[j])) { ops.push(["=", b[j]]); i++; j++; }
    else if (j < m && (i >= n || dp[i][j + 1] >= dp[i + 1][j])) ops.push(["+", b[j++]]);
    else ops.push(["-", a[i++]]);
  }
  return ops;
}

// text nodes of the new version with their start in the text: [node, start]
function textNodes(root) {
  const nodes = [];
  let at = 0;
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, { acceptNode: (n) => (SKIP(n) ? 2 : 1) });
  while (w.nextNode()) { nodes.push([w.currentNode, at]); at += w.currentNode.nodeValue.length; }
  return nodes;
}

export function play(root, before, done) {
  const after = textOf(root);
  if (!root || before === after) { done && done({ added: 0, removed: 0 }); return; }
  let ops = diff(tokens(before), tokens(after));
  if (!ops) ops = diff(before.split(/(?<=\n)/), after.split(/(?<=\n)/)) || [["+", after]];
  // pieces: [kind, position in the new text, text]; neighbours of the same kind are merged
  // a single mark ("-", ","), matching in the middle of an edit, is part of the edit: otherwise it tears a deleted phrase apart
  ops = ops.flatMap((o, k) => (o[0] === "=" && o[1].trim().length <= 1 && ops[k - 1] && ops[k - 1][0] !== "=" &&
    ops[k + 1] && ops[k + 1][0] !== "=" ? [["-", o[1]], ["+", o[1]]] : [o]));
  // a consecutive edit (deleted and inserted interleaved) becomes one deleted piece and one inserted piece, in one place
  const runs = [];
  let pos = 0, del = "", add = "", at = 0;
  const flush = () => {
    if (del) runs.push(["-", at, del]);
    if (add) runs.push(["+", at, add]);
    del = add = "";
  };
  for (const [op, t] of ops) {
    if (op === "=") { flush(); pos += t.length; at = pos; continue; }
    if (op === "-") del += t;
    else { add += t; pos += t.length; }
  }
  flush();
  const real = runs.filter(([, , t]) => t.trim());
  const added = real.filter((r) => r[0] === "+").reduce((n, r) => n + tokens(r[2]).filter((x) => x.trim()).length, 0);
  const removed = real.filter((r) => r[0] === "-").reduce((n, r) => n + tokens(r[2]).filter((x) => x.trim()).length, 0);
  const nodes = textNodes(root), still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const step = Math.max(40, Math.min(160, 2400 / Math.max(1, real.length)));  // "bam-bam": all within a couple of seconds
  real.forEach((r, i) => { r[3] = i * step; });  // in order, top to bottom
  const mark = (cls, delay) => {
    const e = document.createElement("span");
    e.className = cls + (still ? " still" : "");
    e.style.animationDelay = `${delay}ms`;
    return e;
  };
  // from the end: later cuts in the text do not shift the starts of earlier nodes (splitText leaves the start in the node)
  for (const [op, p, t, delay] of [...real].reverse()) {
    const k = nodes.findLastIndex(([, start]) => start <= p);
    if (k < 0) continue;
    if (op === "-") {
      const [node, start] = nodes[k];
      const tail = node.splitText(Math.min(p - start, node.nodeValue.length));
      // a whole paragraph or item is deleted: between blocks it is a block (list item), not a word in the void
      const box = tail.parentNode, list = /^(UL|OL)$/.test(box.nodeName);
      const between = list || box === root || /^(DIV|SECTION|BLOCKQUOTE|TBODY|TABLE)$/.test(box.nodeName);
      const del = mark("mg-del" + (between ? " block" : ""), delay);
      if (list) { const li = document.createElement("li"); li.className = del.className; li.style.animationDelay = del.style.animationDelay; li.textContent = t.trim(); box.insertBefore(li, tail); continue; }
      del.textContent = between ? t.trim() : t;
      box.insertBefore(del, tail);
      continue;
    }
    // added text may span several nodes (bold, link): wrap the piece in each
    for (let q = k; q < nodes.length && nodes[q][1] < p + t.length; q++) {
      const [node, start] = nodes[q];
      const from = Math.max(0, p - start), to = Math.min(node.nodeValue.length, p + t.length - start);
      if (to <= from) continue;
      const mid = node.splitText(from);
      mid.splitText(to - from);
      const add = mark("mg-add", delay);
      mid.parentNode.insertBefore(add, mid);
      add.append(mid);
    }
  }
  const first = root.querySelector(".mg-add, .mg-del");
  if (first) first.scrollIntoView({ block: "center", behavior: still ? "auto" : "smooth" });
  const total = (real.length ? real[real.length - 1][3] : 0) + 2600;
  setTimeout(() => done && done({ added, removed }), still ? 2500 : total);
}
