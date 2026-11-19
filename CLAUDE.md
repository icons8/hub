# agents-hub-core

The core of Hub by Icons8: the link between the owner and a fleet of Claude Code sessions through a browser page and Telegram.
What it is and how to run it is in `README.md`. The environment of a specific machine (where things live, what has already
been decided) is in `CLAUDE.local.md`, if it exists.

Every hub installs this core: each change goes to everyone who updates. Hence the rules below.

## Principles

- State lives only in the core: an event journal plus declarative state machines. Do not guess state from indirect signs in
  several places; everything that shows state reads it from the core.
- The model (Claude Code sessions) only reports facts; hooks only record events. Neither the core nor the hooks steer the
  model. Whatever must be reliable (bringing up sessions, delivery, routing) is done by the core, not by instructions to
  sessions.
- Every known failure gets a deterministic test (an event replay) before the code counts as ready. The test is written
  first.
- Features are modules with a manifest on top of the core; a feature does not write to another component's state, it only
  sends an event.
- Simple and small: the Python 3.12 stdlib by default, a dependency only with a reason. Complexity and fragility are a
  defect: look for a simpler solution; use the standard mechanisms (`hub` commands, the schedule, Edit), not workaround
  scripts.

## What goes where

- Into the core goes only what every hub needs. What one owner needs (their taste, their services, their machine) stays
  local: `CLAUDE.local.md`, their own plugin, their hub's settings.
- Anything optional is a module or plugin, off by default; anything that changes how things work for everyone or sends data
  outside is also off by default. Your own extensions are plugins; into the core goes only what the Plugin API lacks.
- If something similar exists, improve it; do not build a second one next to it.

## Talking to the owner

- A question is not a command: answer it and change nothing. Edit on "do it" or on a clear command; if it is unclear what is
  meant, ask. Do exactly what was asked, and take the owner's decision whole, not in part.
- Before asking, explain: where the task comes from, what the cause is (fix the cause, not the symptom), the options with
  pros and cons, and your opinion. A decision card must be answerable without a follow-up question: what problem it solves,
  what the user will see, whom it affects (the blast radius), what is debatable; the recommended option comes first.
  Decisions come one at a time, not in a batch.
- Show anything visible before asking: a snapshot of the changed place, or before/after. Public text in the owner's name (a
  post, a PR comment) is shown in full before it is sent, with no promises made on the owner's behalf.
- Answer questions about how the core works from the code, recent commits and the journal, not from memory.
- Answer where the question was asked (a comment on a file gets its answer there), in plain words.

## Verification

- Tests: `tools/test.py [module|test …]` runs the whole suite (about 1.5 min) or a part of it, in parallel and with a lock
  against other people's runs; not `python3 -m unittest` directly. Run it before every commit; a new test must fail on the
  old code.
- For a task, a message, delivery, or results, also run against a copy of the journal and check edge cases: no duplicates and
  no losses.
- Rules and session prompts (skills, `hub-home/CLAUDE.md`, the core's addenda) are checked with before/after evals prior to
  release.
- Compatibility before release: hubs of earlier versions (migration by itself, rollback), the Plugin API (the version; old
  plugins keep working), macOS, Linux and Windows, supported Claude Code versions, self-update from an old version.
- Large changes go first onto a copy of a hub with the owner's journal, then onto the owner's hub.
- A fixed regression gets a test or an eval case, so that it does not come back.
- Security outranks convenience: sessions run in bypassPermissions, so name access, keys and data leaving the machine among
  the risks; whatever opens a vulnerability is not released.
- Runs and tests have no real side effects (do not send, do not launch, do not touch a live hub).

## Release

- Publishing happens only on the owner's word; "do not publish yet" holds. To "what is up for release?" have a ready answer
  in plain words: what is in main and unpublished, and whom it affects. Afterwards, right away: what went out and whether a
  restart is needed.
- Only green goes into a release. An entry in `CHANGES.md` under a date: what the user will see, how to enable it, what to do
  to update; for someone else's PR, a "thanks: @login (PR #N)" line.

## Colleagues' PRs

- One at a time: what problem the author solves, whether there is something simpler, whom it affects, whether it is already
  fixed. A bug fix or something small is usually a yes; a redesign or a behavior change for everyone only after agreement;
  something complex needs a spec first.
- Apply nothing until the owner decides.

## Specs

- Anything large starts with a spec: a description of the system as it will be (not "was/now"), mermaid diagrams, worked
  cases, a migration plan; trade-offs go to the owner for a decision. When you edit it, update all the diagrams.

## The owner's decisions

Business decisions, money, external services (creating a bot, buying a server), and how people and agents work are not
yours: bring them to the owner as options, with the recommended one marked, and wait for the answer.
Technical decisions inside an agreed piece are yours; record them (what, why, how to roll back).

## Working

- Commits are small, one meaning each, and the message says what and why.
- Nothing personal in the repository: no names of people, no names of the owner's projects and tasks, no addresses, no keys.
  Examples are neutral. The one exception is the credits: the author line of README.md and the GitHub logins of
  CONTRIBUTORS.md, built by `tools/contributors.py` from the thanks of CHANGES.md.
- Your own executors: collect the result and the lessons and clean up after them; do not take someone else's task, hand it
  back.
- A report to the owner goes in the terminal: what was done, how it was verified (test output), what awaits the owner's
  decision.
