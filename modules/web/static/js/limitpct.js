// The percent of a limits window as the engine's own app says it. Claude Code counts a window up ("53% used"), Codex counts
// it down: its /status says "47% left". The core keeps the share used for both (account.limits, hosts/codex_limits); a
// Codex reading (the fact's engine) is shown as the share left with the word, so the hub and Codex give the same number.
// Pure: the classic page, Clean and its node tests import it. left — the catalog's "{pct}% left" (web.limits.left).
export const countsDown = (limits) => limits?.engine === "codex";

// used — the whole percent used (0–100) the caller already rounded and zeroed for a passed reset
export function pctText(used, down, left) {
  return down ? String(left).replace("{pct}", String(Math.max(0, 100 - used))) : `${used}%`;
}

// the same number without the word, for the one-line limits row: "5 h 80% · week 47%" fits the sidebar footer and a
// phone's status line, the word left goes in the tooltip (pctText)
export const pctNum = (used, down) => `${down ? Math.max(0, 100 - used) : used}%`;
