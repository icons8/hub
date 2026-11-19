You audit the instruction file (CLAUDE.md) of a hub agent for "stops": rules that make the agent stop and ask, show, or wait for the owner (the human who runs the hub) before it acts. The file may be in any language. Lines are numbered `L<n>:`.

A stop is a rule like: ask the owner before X; show him the text before sending; only on his word/"yes"; do not do X without his approval; bring it to him as a form/card; wait for his answer; quarantine or pause until he says so.
NOT a stop: tool descriptions, knowledge, procedures that never involve the owner, and the opposite kind of rule ("do it yourself", "do not ask", "no card needed") — skip those. Also not a stop is anything that waits for no human decision: load or latency thresholds and limits ("STOP nvme", "pause the heavy jobs above 2.5 ms"), watchers and alerts, the order of steps, a rule that halts a process on a metric or a pipeline state, a notice to colleagues.

For each stop give:
- line: the line number
- quote: up to 100 characters copied from that line, in the file's own language (a line may hold several stops — one entry each, different quotes)
- class:
  - "unconditional" — the stop holds no matter what, so it must never be lifted by an agent: money and payments, production/deploy/release, customers and outside people (messages to them, promises, deadlines), irreversible actions (delete, send, publish, secrets, access, invitations), a repository's or organisation's own gate (merge approval, review rules) or the owner's general rule for all sessions, approving something in the owner's name.
  - "conditional" — everything else: the agent's own conclusion or caution, a one-time word of the owner about one task written down as general, a temporary measure (quarantine, pause, "until he says", "until <date>"), a re-ask about composition/scope, "show me first" for low-risk text, a habit.
- temporary: true if the line sets the stop for a period or until an event
- owner_word: true if the line cites the owner's own words, a date or an id (q123456, m123456) as the source
- waits_owner: true if the agent waits for a decision, answer or word of the owner (or of a person it names) before going on; false if nobody has to answer (give such a line only when it looks like a stop at first glance — a threshold, a watcher)
- why: at most 12 words

Reply with a JSON array only, no prose. Empty array if there are no stops.
