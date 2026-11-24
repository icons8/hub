// The page's one clock: the time as the locale writes it, as the native app shows it — a 12-hour locale without a leading
// zero on the hour ("1:09 AM", "1:09 a. m."), a 24-hour one with it ("01:09"); minutes always two digits. Every time on
// the page goes through here (the feed, the Clean theme, the settings lists): "2-digit" put "01:09 AM" in English.
// No DOM: the Clean theme's tests import it in Node.

const hours = new Map();   // locale → the hour option; asking Intl for the hour cycle builds a formatter
function hour(locale) {
  const key = locale || "";
  if (!hours.has(key)) {
    const o = new Intl.DateTimeFormat(locale || undefined, { hour: "numeric" }).resolvedOptions();
    const twelve = o.hourCycle ? o.hourCycle === "h11" || o.hourCycle === "h12" : o.hour12 === true;   // an older engine has only hour12
    hours.set(key, twelve ? "numeric" : "2-digit");
  }
  return hours.get(key);
}

// the options of a time, with the date parts to put before it ({day: "numeric", month: "short"}) — for a formatter made once
export const clockOptions = (locale, date) => ({ ...date, hour: hour(locale), minute: "2-digit" });

// one formatter per locale and date parts: Flow's replay asks every frame, a settings list once a row; made again after a
// minute, as dom.js does with its own: a formatter keeps the time zone it was made in (the computer moved to another zone)
let formats = new Map(), formatsAt = 0;
// ts — seconds (a number, or one in a string, as the old ts * 1000 took it), or an ISO string (a plugin may hold those:
// hub.clock); date — the date parts before the time, none for the time alone. Not a time — "" (Intl throws on it, and one
// row would break the whole list)
export function clock(ts, locale, date) {
  if (typeof ts === "string" && ts.trim() && Number.isFinite(Number(ts))) ts = Number(ts);
  const d = typeof ts === "number" ? new Date(ts * 1000) : typeof ts === "string" ? new Date(ts) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  if (Math.abs(Date.now() - formatsAt) > 60000) { formats = new Map(); formatsAt = Date.now(); }
  const key = `${locale || ""}|${date ? JSON.stringify(date) : ""}`;
  let f = formats.get(key);
  if (!f) formats.set(key, f = new Intl.DateTimeFormat(locale || undefined, clockOptions(locale, date)));
  return f.format(d);
}
