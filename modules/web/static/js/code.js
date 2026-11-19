// Code highlighting (in files and in tool output): highlight.js from vendor, colors are the page tokens
// (css/code.css), light and dark themes handled automatically. No library — plain text, as before.
import { el } from "./dom.js";

/* global hljs */
const ok = () => typeof hljs !== "undefined";
const MAX = 200_000;  // larger — no highlighting: the page must not hang for seconds

const EXT = {
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
  py: "python", rb: "ruby", go: "go", rs: "rust", java: "java", kt: "kotlin", swift: "swift", c: "c", h: "c",
  cc: "cpp", cpp: "cpp", hpp: "cpp", cs: "csharp", php: "php", lua: "lua", r: "r", pl: "perl", sql: "sql",
  sh: "bash", bash: "bash", zsh: "bash", json: "json", jsonl: "json", yml: "yaml", yaml: "yaml", toml: "ini",
  ini: "ini", cfg: "ini", env: "bash", css: "css", scss: "scss", less: "less", html: "xml", htm: "xml", xml: "xml",
  svg: "xml", vue: "xml", plist: "xml", md: "markdown", markdown: "markdown", graphql: "graphql", diff: "diff",
  patch: "diff", mk: "makefile",
};
const NAMES = { Makefile: "makefile", Dockerfile: "bash", Gemfile: "ruby", Rakefile: "ruby" };

// file → highlight.js language or null; without an extension — by the #! on the first line
export function langOf(path, text = "") {
  const name = String(path || "").split("/").pop();
  const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
  const lang = NAMES[name] || EXT[ext];
  if (lang) return lang;
  const bang = String(text).match(/^#!.*\b(python3?|bash|sh|zsh|node|ruby)\b/);
  return bang ? { python: "python", python3: "python", node: "javascript", ruby: "ruby" }[bang[1]] || "bash" : null;
}

// text → highlighted HTML (escaped) or null
export function highlight(text, lang) {
  text = String(text ?? "");
  if (!ok() || !lang || !hljs.getLanguage(lang) || text.length > MAX) return null;
  try { return hljs.highlight(text, { language: lang, ignoreIllegals: true }).value; } catch { return null; }
}

// <code> with highlighting, plain text without it
export function codeEl(text, lang, cls) {
  const c = el("code", cls || null);
  const html = highlight(text, lang);
  if (html == null) c.textContent = String(text ?? ""); else { c.innerHTML = html; c.classList.add("hljs"); }
  return c;
}

export function pre(text, lang, cls) {
  const p = el("pre", cls || null);
  p.append(codeEl(text, lang));
  return p;
}

// highlighted HTML → per line: a span opened on one line is closed at its end and reopened on the next
function splitLines(html) {
  const out = [];
  let open = [], cur = "";
  for (const m of html.matchAll(/(<span[^>]*>)|(<\/span>)|(\n)|([^<\n]+)/g)) {
    if (m[1]) { open.push(m[1]); cur += m[1]; } else if (m[2]) { open.pop(); cur += m[2]; } else if (m[3]) {
      out.push(cur + "</span>".repeat(open.length));
      cur = open.join("");
    } else cur += m[4];
  }
  out.push(cur);
  return out;
}

// highlighted code lines: [{html}] — for the diff and Read output, where every line has its own mark
export function lines(text, lang) {
  const html = highlight(text, lang);
  if (html == null) return String(text ?? "").split("\n").map((l) => ({ text: l }));
  return splitLines(html).map((h) => ({ html: h }));
}

function line(cls, mark, l) {
  const s = el("span", "ln " + cls);
  s.append(el("span", "mk", mark));
  const c = el("span", "tx");
  if (l.html != null) c.innerHTML = l.html; else c.textContent = l.text;
  s.append(c, document.createTextNode("\n"));
  return s;
}

// edit diff (Edit/MultiEdit): − old, + new, each highlighted as a whole — multiline strings and comments stay correct
export function diffEl(edits, path) {
  const lang = langOf(path);
  const p = el("pre", "diff hljs");
  for (const e of edits) {
    for (const l of lines(e.old_string || "", lang)) p.append(line("del", "- ", l));
    for (const l of lines(e.new_string || "", lang)) p.append(line("add", "+ ", l));
  }
  return p;
}

// Read output: "   12→text" — the number is separate, the text is highlighted by file type
const NUMBERED = /^(\s*\d+)(→|\t)/;
export function readEl(result, path) {
  const rows = String(result).split("\n");
  if (!rows.length || !NUMBERED.test(rows[0])) return pre(result, langOf(path));
  const nums = [], body = [];
  for (const r of rows) {
    const m = r.match(NUMBERED);
    nums.push(m ? m[1] : "");
    body.push(m ? r.slice(m[0].length) : r);
  }
  return numbered(body.join("\n"), langOf(path), nums);
}

// code with line numbers (its own from the Read output, otherwise from 1)
export function numbered(text, lang, nums, cls) {
  const p = el("pre", "numbered hljs" + (cls ? " " + cls : ""));
  lines(text, lang).forEach((l, i) => p.append(line("", String(nums ? nums[i] : i + 1).padStart(5) + "  ", l)));
  return p;
}

// ```lang in markdown: marked sets class="language-…"
export function highlightMd(root) {
  if (!ok()) return root;
  for (const c of root.querySelectorAll("pre > code")) {
    const lang = (c.className.match(/language-([\w+-]+)/) || [])[1];
    const html = highlight(c.textContent, lang && hljs.getLanguage(lang) ? lang : null);
    if (html != null) { c.innerHTML = html; c.classList.add("hljs"); }
  }
  return root;
}
