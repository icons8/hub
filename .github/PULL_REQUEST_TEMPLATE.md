<!-- If a maintainer invited you to contribute, or asked you for this very change, carry on: the template below is for you. -->
<!-- Anyone else: pull requests from outside the maintainers' group are not accepted for now (see CONTRIBUTING.md). Please open an issue
instead: a good issue is the most useful contribution. -->
<!-- The core takes only what every hub needs; anything else is a module or a plugin (see EXTENDING.md). Before you open this,
go through "Before you open a pull request" in CONTRIBUTING.md: most pull requests that come back fail one of its checks. -->

## What problem does this solve?

<!-- The cause, not the symptom. Link the issue if there is one. Whose problem is it: every hub, or your own setup? -->

## Old behavior

<!-- In detail, as the person or a session saw it before this change: what they did, what happened, what they read on the page,
in Telegram, in `hub` output or in the session's prompt. Commands, texts, numbers. "It was broken" is not enough. -->

## New behavior

<!-- The same steps after this change: what they see and read now. Everything else that changes as a side effect (a new
event, a field, a default, a text that sessions get, a Plugin API point) is listed here too, even if it seems small. -->

## How reproduced

<!-- For a bug: the steps or the journal events that show it on the current main, and that the same steps no longer show it with
this change. For a speed or size gain: the measurement before and after, on what data and machine. Not a bug and no gain
claimed: "not applicable". -->

## Who is affected

<!-- Every hub at once / only those who turn it on (which setting) / only you. What it touches: the core's state, delivery,
the page, Telegram, the texts sessions get, model turns or money. How to roll it back. -->

## Tests

<!-- Which test fails on the old code and passes on the new. A hub installed from git runs the suite on its own machine when it
updates: see "Portability: tests run on other machines" in CONTRIBUTING.md. Tick what you ran, write "not checked" next to the
rest. -->

- [ ] Linux: `tools/test.py` passes (or: not checked)
- [ ] macOS: `tools/test.py` passes (or: not checked)
- [ ] Windows under a standard account, not an administrator: `tools/test.py` passes (or: not checked)

## Checklist

- [ ] The branch starts from the current main, and nothing in it is already in main or in another open pull request.
- [ ] One change: unrelated fixes are separate pull requests.
- [ ] A test fails on the old code and passes on the new (for a bug: a replay of the events that broke).
- [ ] The tests follow the portability rules in CONTRIBUTING.md: no links without the `can.symlink()` probe, no `shutil.rmtree` on a folder git wrote, no `os.kill(pid, 0)`, no fixed `sleep` waits or wall-clock limits, no `sh`/`rm` through `subprocess`.
- [ ] Code that stops processes, deletes files or writes outside the hub's folders has a test for the edge (a pid of another program, a link to someone else's file), not only the happy path.
- [ ] A change to the page works while the previous server still runs (an update serves new files before the server restarts).
- [ ] A new API point (a path, a snapshot key, an event, a slot) is in EXTENDING.md with its version in `core/version.py`; a new setting is in EXTENDING.md; a new `hub` command or flag is in its help.
- [ ] A change to text that sessions get (skills, prompts, "For sessions" lines) comes with evals, before and after.
- [ ] No personal data, keys, internal addresses, machine-specific paths or examples from your own work in code, tests or texts.
- [ ] New behavior that sends data off the machine, or changes behavior for everyone, is off by default.
