// When a recording stops by itself — the thresholds in one place (the apps use the same numbers) and the decision itself,
// with no browser in it: the caller hands in the sound level and the time, so a test feeds it a made-up clock.
//   silence — the sound was there, then SILENCE_MS of quiet in a row: stop and use the recording as usual;
//   nosound — SILENCE_MS passed and there was no sound at all: stop too (the page sends it like "silence" — the
//             transcription, not the level, says "no speech");
//   max     — MAX_MS of recording: stop and use it.
export const AUTOSTOP = Object.freeze({
  SILENCE_MS: 20_000,   // quiet in a row that ends the recording
  MAX_MS: 600_000,      // the longest recording (10 minutes)
  QUIET_DB: -50,        // RMS level (dBFS) at or below which it is quiet; the browser's noise suppression keeps a room under it
  TICK_MS: 250,         // how often the level is read
});

// RMS level of a block of samples (-1…1) in dBFS: 0 is full scale, silence is -Infinity
export function levelDb(samples) {
  if (!samples.length) return -Infinity;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return 20 * Math.log10(Math.sqrt(sum / samples.length));
}

// the state of one recording; listening: false — the level cannot be read (only the time limit works)
export const newWatch = (now, listening = true) => ({ since: now, quietSince: now, heard: false, listening });

// one reading: the level in dB (null — not read this time) at time `now` (ms) → "" | "silence" | "nosound" | "max"
export function step(w, db, now, cfg = AUTOSTOP) {
  if (db == null) w.quietSince = now;   // no reading (the audio context is not running): nothing is judged by that
  else if (db > cfg.QUIET_DB) { w.heard = true; w.quietSince = now; }
  if (w.listening && now - w.quietSince >= cfg.SILENCE_MS) return w.heard ? "silence" : "nosound";
  if (now - w.since >= cfg.MAX_MS) return "max";
  return "";
}
