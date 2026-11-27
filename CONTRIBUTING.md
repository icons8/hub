# Contributing to Hub by Icons8

Thank you for wanting to help. The project is deliberately small at the center and open at the edges, so the most useful
thing to know first is **what helps right now**.

## Pull requests are welcome

We read and review pull requests from everyone. The team is small, so a few habits keep it quick for both sides:

- **Open an issue first for anything larger than a small fix.** A bug fix of a few lines, a typo or a missing test can
  go straight to a pull request. A new feature, a change of behavior or a refactor starts with an
  [issue](https://github.com/icons8/hub/issues/new/choose) (or a post in
  [Discussions](https://github.com/icons8/hub/discussions)): we tell you whether the core is the place for it, so
  nobody spends a day on something we would not take. Much of what people want is a module or a plugin, and those live
  in your own repository ([EXTENDING.md](EXTENDING.md)).
- **One change per pull request.** Unrelated fixes are separate pull requests.
- **A bug fix comes with a test that fails without the change.** A replay of the events that broke is best.
- **`tools/test.py` passes** (see [Tests](#tests)), and the new tests follow the
  [portability rules](#portability-tests-run-on-other-machines).
- **English, and neutral examples.** Code, comments, tests and texts in English; no names of people or companies, no
  private paths, addresses or keys, in examples too. A change to the page comes with a picture of the changed place.
- **Fill in the pull request template**: what a person saw before, what they see now, how you reproduced it, and who is
  affected.

Go through [Before you open a pull request](#before-you-open-a-pull-request) below first: most pull requests that came
back failed one of its checks.

**What happens next.** A maintainer reads the pull request and answers in it. The answer is one of: merged; changes
asked for (we explain what, a rebase onto the current main may be needed); or closed with the reason, for example
when the change belongs in a module or a plugin, or is already planned another way. Reviews are done by people, not on a
schedule, so a pull request may wait: a short comment asking for news is fine. We may also port your change into our own
tree and close the pull request with a thanks, when that is quicker than another round; the change still reaches
everyone, and you are still credited. Merged or ported, a change goes out with the next release of the hub.

**Credit.** Every release adds an item to [CHANGES.md](CHANGES.md) for each change it carries, and an item from outside
the team ends with "thanks: @your-login (PR #N)". The list in [CONTRIBUTORS.md](CONTRIBUTORS.md) and the README's
Contributors section are built from those thanks by `tools/contributors.py` at the release, and the release commit
names the people thanked in it as co-authors. A bug report or an idea that led to a change is thanked the same way
("issue #N"). The release commit of the public repository is made by the maintainers, so your own commits are not kept
one by one in its history; the credit above is what stays.

Contributing is far from only code. What works as well:

- **Report a bug.** Open an [issue](https://github.com/icons8/hub/issues/new/choose) with the bug form: the
  version (the "About" page, or `git rev-parse HEAD`), your OS, what you did, what you expected, what happened, and the
  output of `hub doctor`. Do not paste the journal, tokens or private paths: cut them out first. A bug that you can
  reproduce in a few steps gets fixed fastest.
- **Propose a change** with the feature form: the situation you are in, what you would like the hub to do, and what you
  tried instead (a module, a plugin or a skill may already do it). A blank issue is possible too, but a report with these
  facts gets answered fastest.
- **Share an open-ended idea or ask a question** in [GitHub Discussions](https://github.com/icons8/hub/discussions).
  Upvote the ones you care about. The team is small and may not answer every post, but reads them and weighs them when
  planning.
- **Build a module or a plugin** in your own repository ([EXTENDING.md](EXTENDING.md)): a module installs straight from
  its git URL (`hub module add`), and a page plugin with the GitHub topic `agents-hub-plugin` is found by the plugin
  catalog. If the Plugin API is missing something you need, say so in Discussions: that gap is the best kind of core
  improvement.
- **Write guides, blog posts and examples**, and answer other people's questions.
- **Report a security problem or a conduct problem privately** with GitHub's private reporting, as described in
  [SECURITY.md](SECURITY.md); never in a public issue.

## Where a change belongs

The rest of this file is for anyone who sends a change or reads the code.

- **The core** (`core/`, `bin/hub*`, the session host, the base of the web page) is installed by every hub, so every change
  reaches everyone who updates. It takes only what every hub needs: bug fixes, reliability, security. Anything that suits
  one person's taste, services or machine does not go here.
- **Modules** (`modules/`) are optional features on top of the core. A new feature is a module with a manifest that reads
  the journal and sends events; it never writes into another module's state.
- **Plugins, skills, themes and MCP servers** are the preferred way to extend the hub. They live outside the repository,
  in the user's own folder, and do not interfere with updates.

Optional behavior is off by default. Anything that sends data off the machine, or changes behavior for everyone, is off by
default too.

## Before you open a pull request

Most pull requests that came back, or were closed, failed one of these checks; each takes minutes before sending and
saves a round of review.

- **Is it for every hub?** A change for your own workflow, service or app belongs in a setting, your own `CLAUDE.md`,
  the `rules` list in `hub.json`, a module or a plugin, not in the core. A new API point needs a user in this
  repository, not only in your client.
- **Is it already there?** Fetch and look at the current main and the open pull requests (by the feature's name and by
  the files you touch). Start the branch from the current `origin/main`, not from your hub's merged copy, and do not
  build on your own unmerged pull requests.
- **Is the bug real on main?** Reproduce it on the current main in real conditions (a real Claude Code, a real journal)
  and write down how. A claimed speed or size gain comes with a measurement before and after. A bug that does not
  reproduce, or cannot happen by the code, is closed.
- **One change.** Unrelated fixes, a feature on top of a fix, a "daily batch" — separate pull requests. A partial yes
  then does not mean splitting your commits by hand.
- **The edges, not only the happy path.** Replay the change over a copy of a real journal when it touches messages,
  tasks or delivery: no duplicates, no losses, no extra model turns. Feed a new setting `"abc"`, `-1`, `0`, `null` and a
  huge number: the core must not crash on a typo. Try paths with spaces, links, two open tabs, a restart in the middle.
- **The price for everyone.** Count what the change adds on every hub: messages, model turns, load. New behavior is off
  by default behind a setting unless every hub needs it.
- **Session texts need evals.** A change to what sessions read (skills, prompts, "For sessions" lines in `CHANGES.md`)
  comes with `evals/prompts.py` cases run before and after, on the rule's own cases and its neighbors.
- **The page during an update.** An update serves new page files before the server restarts: the page must work with the
  previous server (a missing field, a missing path). A new point of the page's or the modules' API (a path, a snapshot
  key, an event, a slot) goes into [EXTENDING.md](EXTENDING.md) with the version it belongs to in `core/version.py`
  (`PLUGIN_API` or `MODULE_API`), a new setting into EXTENDING.md too; a new command or flag of `hub` goes into its
  help, `core/hubhelp.txt`.
- **Tests that prove it.** A test fails on the old code because of the behavior, not because of a renamed function. Data
  shapes come from the real program or journal, not made up. Tests follow the portability rules below.
- **Nothing personal.** No names, company, your projects, quotes or examples from your own work in code, comments, tests
  or texts that every hub gets.
- **Describe the old and the new behavior.** Fill every section of the pull request template: what a person or a session
  saw before, in detail (commands, texts, numbers), what they see now, how you reproduced it, who is affected, which
  tests ran where. The reviewer and the maintainer who decides read only this.
- **Rebase when asked**, onto the current main, and run the whole suite again.

## Building and running

The hub is Python 3.12 using only the standard library, plus a web page written as plain ES modules (no build step). A
dependency needs a stated reason.

```sh
git clone <repository-address> && cd agents-hub
bin/hub-up          # the core and modules in one process; the page is at http://127.0.0.1:8787/
```

Installers for macOS, Linux and Windows are in `deploy/`; see the [README](README.md). You need Claude Code installed to run
sessions. Tests start no real sessions and have no real side effects.

## Tests

```sh
tools/test.py                  # the whole suite, in parallel
tools/test.py test_reducer     # one module or test
```

Use `tools/test.py`, not `python3 -m unittest` directly: it runs in parallel and holds a lock against concurrent runs.

- Write the test first. For a bug, the test is a replay: the recorded sequence of events that used to break. It must fail
  on the old code.
- A change to messages, tasks, delivery or summaries also gets a run over a copy of a real journal and the edge cases: no
  duplicates, no losses.
- Changes to the rules and prompts that sessions receive come with evals, before and after.
- Run the whole suite before every commit.

## Portability: tests run on other machines

A hub installed from git runs `tools/test.py` on its own machine when it updates, as a self-check, and a red test
there holds back that hub's update. So a test has to pass on macOS, Linux and Windows, on a slow or busy machine, and
on Windows under a standard account (not an administrator, no Developer Mode), not only where it was written. Most
tests that failed on other people's machines were not wrong about the hub; they assumed the author's machine.

- **The lint.** `tools/portability_lint.py` (run by `tests/test_portability_lint.py`) turns most of the rules below red
  in a new test; a deliberate case is marked `# portable: <why>` on its line, on a comment line just above it or on
  its `def`.
- **Symbolic links.** A standard Windows account cannot create them (WinError 1314). A test makes a link through
  `tests/winsafe.py`, which asks the probe `can.symlink()` from `tests/fakes/can.py`, not `os.name` (an administrator or
  Developer Mode can): `winsafe.symlink(self, …)` skips the test without the right, `winsafe.try_symlink(…)` returns
  False there for a test whose other checks hold without the link.
- **Replacing an open file.** On Windows `os.replace` over a file that another handle holds open fails (WinError 5).
  Close it first; a test about exactly that skips by `can.replace_open()`.
- **Removing a folder that git wrote.** Use `platforms.current.rmtree`, not `shutil.rmtree`: git leaves read-only files
  in `.git`, and on Windows `shutil.rmtree` cannot remove them.
- **Is a process alive.** Never `os.kill(pid, 0)`: on Windows signal 0 is Ctrl+C, sent to the whole console. A test asks
  `alive()` from `tests/fakes/procs.py` (a zombie counts as dead); the hub's code asks `platforms.current.alive`, and code
  that stops a process goes through `platforms/` too.
- **Text.** Pass `encoding="utf-8"` to `open()` and to `subprocess` with `text=True`. A file that git reads or that a
  test compares byte for byte is written with `newline="\n"`: Windows text mode writes `\r\n`.
- **Paths.** Use `/` in config files (git config), inside git (`git show <sha>:<path>`) and in URLs; build a file URL
  with `pathlib.Path.as_uri()`. No hard-coded `/tmp` or `/dev/null`: `tempfile`, `os.devnull`.
- **Programs.** Do not run `sh`, `bash`, `rm` or `echo` through `subprocess`; do it in Python. A fake program is made with
  `tests/fakes/exe.py`: on Windows it is a `.cmd` wrapper, and cmd.exe splits an unquoted `&`, as in a URL.
- **Time.** No fixed `sleep` waits and no wall-clock limits such as `assertLess(time.time() - start, 5)`: a busy machine
  misses them. Wait for the condition itself, with a generous deadline: `until()` from `tests/waiting.py` returns as soon
  as the condition holds, and `HUB_TEST_SLOW=3` stretches its deadlines on a slow machine. Do not depend on the number
  of CPU cores, the locale, the time zone or the Python version (3.12 is the minimum).
- **The machine's git.** A test makes its own temporary repository. It does not read the history of the hub's own copy:
  that copy differs from hub to hub (history, local edits, the user's git settings).
- **Code that can touch what is not the hub's.** Code that stops processes, deletes files or writes outside the hub's
  folders gets a test for the edge, not only the happy path: a pid that now belongs to another program, a link that
  points at someone else's file, an empty path or the home folder. The test shows that the code leaves them alone.

Run the new and changed tests on every system you have, Windows under a standard account if you can. The pull request
template asks which systems ran them and which did not; "not checked" is a fine answer.

## Principles

- State lives only in the core: an event journal plus declarative state machines. Do not infer state from indirect signs in
  several places; everything that shows state reads it from the core.
- The model only reports facts; hooks only record events. Neither the core nor the hooks steer the model. Reliable things
  (starting sessions, delivery, routing) are done by the core, not by instructions to sessions.
- Simple and small. Fragility and complexity are defects; look for the simpler solution.
- Security over convenience. Sessions run in `bypassPermissions` (see [SECURITY.md](SECURITY.md)); name the access, keys and
  data your change touches, and do not weaken the approval gates.
- Keep the repository free of personal material: no names, internal addresses, keys or machine-specific paths in code,
  tests, documents or screenshots. Use neutral examples. `tools/dev/readme_demo.py` makes the screenshots in `media/` from an
  invented fleet and checks the text of every page it shoots.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
