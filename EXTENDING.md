# How to extend your hub

Your hub is a copy of the shared repository. It updates itself (once an hour, if the tests are green), so it is best to
keep your own changes **outside the hub code**; then updates never conflict with them. Below is what you can do without
editing the code, from simple to complex. Your **Hub Developer** agent (`hub-dev`) can do all of it: tell it what you need.

| I want to | How | Where it lives |
|---|---|---|
| change hub behavior: projects, time zone, browsers, Telegram, voice, rules for all sessions (`rules`) | settings | `~/.config/hub-core/hub.json` |
| give sessions skills, their own agents, commands | skill set (Claude Code plugins) | your own git repository |
| give sessions a service (Jira, databases, your own API) | MCP server | `hub.json` → `mcp` |
| run your own background process: watch, count, write to the hub | core extension | your own folder |
| a process the core calls itself at its own points | core module (Module API) | your own folder |
| your own tab on the hub page | page extension | your own folder |
| install someone else's plugin or theme, publish your own | plugin marketplace | the author's GitHub repo |
| change the hub itself, for yourself | a personal commit in your copy | local branch, never leaves your machine |
| change the hub for everyone | branch and PR to the shared repository | accepted by the shared hub's maintainer |

## Skill sets and MCP for sessions

A **skill set** is a git repository in the Claude Code marketplace format (`.claude-plugin/marketplace.json`, plugins
with skills, agents, commands). To connect one:

```json
"sets": [{"name": "my-skills", "path": "/path/to/my-skills"}]
```

The set's plugins appear in the catalog (`hub tools list`) as `<plugin>@my-skills`; the risk is in the plugin's
`keywords` (`risk:money`, `risk:prod`, `risk:owner-data`, `risk:external`), and a risky plugin is connected only with your
"yes". A plugin for all sessions goes in `"base"`, a plugin only for the hub goes in `"hub_tools"`.

An **MCP server** is declared as in `.mcp.json`, plus a description and a risk:

```json
"mcp": {"my-api": {"server": {"command": "npx", "args": ["-y", "my-mcp"]},
                   "description": "Our orders API: read-only", "risk": ["owner-data"]}}
```

A session asks for the tool itself (`hub tools request my-api "why"`), and the hub connects it.

`"as": "<name>"` connects the server to sessions under that name instead of the id: Claude Code keeps a sign-in (OAuth)
under the server's name, so an entry named as the server you already signed in to (`plugin:<plugin>:<server>`) needs no
new sign-in. An `as` that is another tool's id, another entry's `as` or the name of a plugin server granted to the same
session, or a name with spaces around it or a control character, is dropped with a log line, and the entry is connected
under its id. `mcp_login_confirm` with the entry's id
covers its `as` too.

`"mcp_deny": ["plugin:<plugin>:<server>"]` (off by default, an empty list) switches off one MCP server a plugin brings, on
this machine: sessions started after the change do not get it, the plugin's other servers, skills and commands stay.
Use it for a server that needs a token you have not set (a 401 in every session). It names only plugin servers: a
catalog server (`mcp`) with the same name, a name that matches nothing and an entry that is not a string are left
alone. It does nothing with `"connectors": true`: then the core passes no `--strict-mcp-config`, and Claude Code
starts the plugin's own servers itself.

## Core extension: your own process next to the hub

```json
"extensions": {"hello": {"path": "~/hub-ext/hello", "run": ["python3", "main.py"], "web": "web/main.js"}}
```

`hub-up` starts `run` in the `path` folder as part of `ext:hello` and restarts it if it crashes; restart it manually with
`hub restart ext:hello`. Environment: `HUB_STATE_DIR` (the hub's state folder), `PYTHONPATH` (the hub code),
`HUB_EXTENSION` (the name). Python 3.12, standard library only, same as the core.

**Reading**: the event journal, read-only:
- `core.journal.read_tail(path)` returns the latest events without reading the whole journal;
- `core.tail.Tail(path)` reads on the fly; `Tail.reader` (`core.views.Reader`) holds the hub state (sessions,
  messages, tasks, schedule) and the session feeds; after the journal is rotated it starts over by itself;
  `Tail(path, page=False)` skips building the page snapshot (`Tail.snapshot` stays `None`) on every new event —
  for a reader that needs only `Tail.reader.state`;
- `core.journal.load_state(state_dir)` returns the current state in one call.

A session state without a process (API 1.30) is `asleep` (sleeping), `parked` (free without a process: free as far as
the owner is concerned, and a message will bring it up) or `retired` (an executor that handed in its task or was
stopped: messages do not wake it); previously the last two were `asleep` with a `parked` or `retired` field. The page
snapshot and the module slice keep the old shape.

**Writing**: only facts, into the core's spool: `core.spool.put(<state>/spool, type, entity, data, src="ext:<name>")`.
The core writes the fact to the journal and decides what to do with it. The core sets the channel and the sender: a fact
from an extension (or a module) always has `channel: "plugin"`, `owner: false`, `sender: "ext:<name>"` (for a module,
`mod:<name>`). An extension cannot speak for a session, the hub, the core or the owner; the core replaces any `channel`,
`owner` and `sender` you set. The following are open to an extension:

| Fact | What happens |
|---|---|
| `ext.<name>.<what>` | just a journal entry (statistics, observations); visible in `hub show --grep` |
| `owner.notify` `{title, text, urgent}` | a message to you in Telegram and on the page |
| `owner.notify` with `id`, `topic`, `urgency`, `due`, `session`, `push` | the same, plus a phone push (see "Owner notification: push and resolve" below) |
| `owner.notify_resolved` `{id}` | the reason is gone: the notification is resolved and the phone banner is removed |
| `message.received` `{to: <session>, text}` | a message to a session; its label reads "from plugin <name>", and on the page "Plugin <name>" |

An extension does not write to the journal itself and does not change other components' state. This is a core principle:
state lives only in the core.

The spool checks every fact (`core/admit.py`). These are flagged: facts that only the core writes (`turn.result`,
`session.said`, `permission.requested`, `session.identified`, etc.), the core channel (`core`), a fact without a channel
(except facts from the hook, the clock, the browser, the gateway, `module.error`, `module.model`, `ext.*`, `mod.*`), and a
sender that is a session that does not exist. For now a fact that fails such a check is written as before, and the core
also writes a `spool.flagged` entry to the journal with the reason; the next release will reject it instead.
Name the channel of your own script that puts facts into the spool `cli` (on your behalf) or `plugin` with a `sender`.

The main journal events (`{seq, ts, type, entity, data}`): `message.received` (to whom, text, from whom),
`turn.started` / `turn.tool` / `turn.result` (a session turn: start, step, outcome), `delegation.created` (task dN; the
event name is the old one), `session.requested` / `session.started` / `session.exited` (session life),
`permission.requested` (a session asks), `schedule.*` (the schedule), `owner.file` (a file to the owner: `path`,
`caption`, `sender`; `screenshot: true` since 1.36 when the sender says it is a screen capture — a `hub shot` PNG says so
itself: a tEXt chunk `Software` = `hub shot` right after IHDR).

### Owner notification: push and resolve (Module API 2.5)

Besides `title`, `text` and `urgent`, `owner.notify` (`hub notify`, `hubmod.fact`, the spool) accepts optional fields:

- `id` (a string of up to 128 characters): while the notification is open, the same `id` updates it. On the phone the
  banner is replaced; in Telegram a new message is sent. Without an `id` every notification is new.
- `topic`: the topic for the push (the title, up to 40 characters); without it, `title` is used.
- `urgency` is `now` or `later`; `due` is the deadline (a string of up to 40 characters, or a time in seconds). The push
  starts with "Now: by <due>." / "Can wait.". Only the author writes the urgency: neither the core nor the model derives
  it from the text.
- `session`: an existing session; tapping the push opens it.
- `push: false`: Telegram and the feed only, no push.

`owner.notify_resolved {id}` resolves your own notification (a client that supports `clear` removes the banner from the
phone). A module can resolve its own; the hub and the owner can resolve any notification by its full id
`n:<sender>:<id>` (a module is `mod:<name>`, a plugin process is `ext:<name>`, a schedule job is `schedule:<job>`; with
pushes enabled the id is also visible in the snapshot's `alerts`); resolving someone else's is rejected. A module may
write this event without `core.write`, just like `owner.notify`. An open notification lives for a day, and there can be
at most 50 open ones. From the command line: `hub notify --id <id> [--topic T] [--urgency now|later] [--due DEADLINE] [--session S] [--no-push] <text>` and `hub notify --resolve <id>`. In a push, a module's notification always starts with
its name, so it cannot pass itself off as a session's question; there is no address (`url`) in a push. The limit is 6
banners per hour per sender; after that a single replaceable "<sender>: N more" banner is shown.

## Core modules: Module API 2.10

A module is a folder with `module.json` and its own process, in any language. Unlike an extension, a module is called by
the core itself: at the points the module declared, over local HTTP, and the core waits for the answer no longer than the
point's budget. If the module does not answer, answers incorrectly or its process crashes, the core does its own thing
and the module is suspended; the core and the sessions do not fail because of it. A module is code with the hub's rights
(it reads the hub's files and can reach the network): you are the one who enables it.

### Getting started: a Python module in five minutes

```
~/hub-modules/notes/module.json:
{"name": "notes", "version": "0.1.0", "api": "^2.3", "run": ["python3", "main.py"], "capabilities": ["http", "doctor"]}
```

```python
# ~/hub-modules/notes/main.py
from core import hubmod

@hubmod.hook("doctor")                                # a core point: POST /hook/doctor
def doctor(req):
    return [{"name": "notes", "ok": True}]

@hubmod.route("/notes", methods=["GET", "POST"])     # a page endpoint: /api/mod/notes/notes
def notes(req):
    if req.method == "POST":
        hubmod.state.set("list", hubmod.state.get("list", []) + [req.json()])   # own state in the hub journal
    return {"list": hubmod.state.get("list", [])}

@hubmod.view("/")                                     # an HTML view: /mod/notes/view/
def page(req):
    return "<!doctype html><pre id=o></pre>" + hubmod.VIEW_JS + \
           "<script>hub.json('/notes').then(r => o.textContent = JSON.stringify(r))</script>"

if __name__ == "__main__":
    hubmod.run()
```

```
hub module add ~/hub-modules/notes --dev     # from disk, in developer mode: editing a file restarts the module
hub modules                                  # state, failure reason, process log
```

`hubmod` (`core/hubmod.py`) runs the server itself, checks the secret, answers `hello` from `module.json`, validates a
point's response against the core's schema (if it does not match, the reason goes to the module's log) and, before
starting, checks the rights for the points and endpoints (if they are missing, the process exits with a reason). A
complete example is in `examples/module-hello`.

- `@hubmod.hook("<point>")`: a function `(JSON request) -> JSON response` (the points are in the table below).
- `@hubmod.route("/path", methods=[…], stream=False, body="memory")`: an endpoint (the `http` right). The path may
  contain `<name>`, available as `req.params["name"]`. Request: `req.method`, `req.path`, `req.query`, `req.headers`,
  `req.json()`, `req.text()`, `req.body`, `req.owner` (passed the owner check), `req.view` (sent by the module's view).
  Response: dict/list is JSON, str is text, bytes, `None` is 204, `(status, body)`,
  `hubmod.Response(body, status, headers, type)` or `Response(file=path)`; `raise hubmod.HTTPError(409, "text")` is an
  error with a reason. `stream=True` makes the function a generator, each value being a `text/event-stream` event (pings
  are automatic); `body="file"` puts the body in a temporary file `req.file` (deleted after the response), with no memory
  limit.
- `@hubmod.view("/path")`: an HTML view; `hubmod.VIEW_JS`: insert it into the HTML so that the view can call its own
  endpoints (`hub.call`, `hub.json`; see "The view calls its own endpoints" below).
- `hubmod.state` is the module's own state: `get(key, default)`, `all()`, `set(key, value, if_rev=None)`,
  `delete(key)`, `refresh()`. A write is a `mod.<name>.set` fact in the spool; the process sees its own write at once,
  and the limits (256 KB per module, 512 KB for all) are checked before the spool, raising `ValueError` with a reason. A
  write that the core nevertheless rejected (wrong `if_rev`, the module has not answered `hello` yet) leaves
  `core.rejected` in the journal, and the process's copy keeps it until `refresh()`. `get` and `all` return a copy, and
  `set` takes a copy in the form the value will have in the journal (a tuple becomes a list, keys become strings; 2.6):
  editing a dict you read or wrote does not leak into the process state. Before 2.6 `get` returned the object itself, so a
  rejection in the middle of editing it left a half-modified value in the process, and the next write carried it into the
  journal.
- `@hubmod.on_ready`: a function with no arguments, called once per process, when the core has recorded that the module is
  ready in this run (after `hello` and migration; if paused, after the pause), in its own thread. Use it to rebuild the
  snapshot keys (2.4, below).
- `hubmod.fact(type, data, entity=None, dedup_key=None, wait=None)`: a fact from the module; `hubmod.settings()`: the
  hub.json section named after the module. With `dedup_key` (2.6, a string of up to 128 characters) a repeat creates no
  duplicate: the core rejects the same key from the module within a day (below, "Repeat without a duplicate"). With
  `wait=seconds` (2.6, up to 30) it waits for the core's decision, like `hubmod.owner`: `(seq, None)`, `(seq, reason)` or
  `(None, None)`. The rejection code is the `.code` field of the reason: a repeat is `"duplicate"`. An invalid key, `wait`,
  or your own `req` field together with `wait` raises `ValueError` before the spool.
- `hubmod.data_dir()` (2.4): the module's own data folder `<state>/moddata/<name>/` (0700, created by the call): caches,
  indexes, its own files. The module folder is code (in developer mode, editing it restarts the module), and the journal
  is not for data.
- `hubmod.servable(path)` (2.4): whether the page's `/api/file` serves this path: the real path (after `realpath`) or
  `None`. The rule is the same as the page's: an owner's attachment, a file sent by a session (`hub send-file`), or an
  image it named in a step or text, across the whole journal history, and the file exists on disk. The first call reads the
  ready parses of the journal archives (the unparsed ones are parsed in the background, and until then their paths are
  `None`); later calls read only what was appended. It keeps the paths themselves in memory. A list of paths (2.6) gives
  `{path: real path | None}`, and the journal is read through once for the whole list (one path per call means once per
  call); a non-string in the list raises `TypeError`. `hubmod.servable_state()` (2.6) says whether the answer is complete:
  `"ready"` means the history is parsed; `"building"` means the archives are still being parsed in the background (their
  paths are `None` for now: ask later); `"partial"` means an archive could not be parsed (the reason is in the module's
  log), so retry no sooner than 10 minutes later.
- `hubmod.owner(req, type, entity, data, dedup_key=None, wait=None)`: the owner's word from an endpoint (below, "The
  owner's word"): only on a request with `req.owner`; from a view or without a request it raises `HTTPError(403)` before the
  spool. With `wait=seconds` (up to 30, 2.4) it waits for the core's decision, like the page buttons: `(seq, None)` means
  accepted, `(seq, reason)` is a rejection in the core's words (show it to the client, usually as a 409 response), and
  `(None, None)` means the core did not record it within `wait` (hubd is busy or not running; the text for the client is
  `hubmod.NOT_DECIDED`). A `req` field in `data` together with `wait` is taken by the core's label and raises `ValueError`.
  A `dedup_key` that is not a string of up to 128 characters raises `HTTPError(400)` before the spool (2.6; the key usually
  comes from client data, so an endpoint that did not catch it answers 400, not 500); it is also a `ValueError`, as before
  2.6.
- `hubmod.session(name)`, `hubmod.sessions()`: the hub's sessions (2.4, the `sessions` right; below, "Hub sessions").
- `hubmod.accounts()`: the limits of the hub's Claude accounts (2.6, the `accounts` right); `hubmod.owner_message("mN")`:
  to whom the owner's message went and what counted as the reply (2.6, the `messages` right; both are covered below in "Hub
  sessions").
- `hubmod.transcribe(audio, type, timeout=None, cancel=None)`: audio to text via the hub's voice provider (2.4, below).
- `hubmod.voice_available()`: whether the hub has voice configured, without calling the provider (2.4, the
  `voice.transcribe` right, below).
- `hubmod.model(prompt, text=…, model="haiku", schema=None, max_tokens=1024, timeout=60)`: a short model call through the
  core, with no tokens held by the module (2.7, the `model` right; the owner grants it; below, "Model through the core").

### A module in a build's embed (Embed API 1.1)

A build of the core with a company's own code (an embed, `embed/<name>/`, core/embed.py) can carry Module API modules:
`embed/<name>/modules/<module>/module.json`. They are built-in modules of the hub, like the core's own: found with no
`hub module add`, on by their manifest (`"default_off": true` — off until `modules.enabled` names it). The "Modules" page
lists them with the third-party ones, with a switch: turning one off names it in `"modules": {"off": [...]}`, turning it
on removes it from there (and adds it to `modules.enabled`). A module named as one of the core's is not started (the
core's stays) and the build refuses it, as it refuses a manifest the hub would block; a name in two embeds — neither.
`hub module dev <name>` works on an embed's module in its own folder; a copy with its name in another folder is blocked.
A `modules/<module>/catalog.py` with `entries(cfg)` in it adds tools to the catalog, as in the core.

### A build's own plugin catalog (Embed API 1.2)

An embed's `modules/private.json` may name its company's plugin catalog: `"repo": "<owner>/<name>"` (a GitHub repository
with `hub-marketplace.json`). Alone, that catalog stands instead of the open one under the official name `agents-hub`
(as before 1.2). With `"catalog": {"name": "<name>", "ref": "<branch>"}` (`ref` optional: the default branch otherwise)
the hub connects both by itself, each official: the open catalog under `agents-hub` and the company's under its own name
(not `agents-hub`, `hub`, `official` or a look-alike, not a built-in third-party name), read from that branch; a
`catalog` that breaks these rules gives no second catalog (the open one stays, the build's tests should catch it). Each
revokes only what was installed from it; an entry name should be in one of them only (`install <name>` without
`@<catalog>` refuses a name both have). A hub that had the company's catalog as `agents-hub` moves to the pair by itself,
once, at the first catalog pass of the new core: what was installed keeps its key, its settings and its on/off; only the
catalog it belongs to (`market`) changes — to the one that lists its entry's name with its own source. Until both the old
catalog and the company's under its new name (on its branch, whose `hub-marketplace.json` says that name) can be read,
the hub stays on the old one.

### Files a build lays in place: `deploy/` (Embed API 1.2)

An embed may carry files under `deploy/` (`embed/<name>/deploy/...`, core/embed.py `PLACED`): what the core does not
carry and the company makes itself, such as its Mac launcher signed with its own Developer ID
(`deploy/mac/<Name>.app`). A build of the team flavor lays each of them at the same path of its layer, byte for byte, a
program still a program; the embed keeps its copy too. A launcher an embed lays into a folder takes the place of the
open flavor's there (`Hub by Icons8.app`, which a tree of the open core has in place). A path the core carries, or one
another embed lays, is refused at the build. The open flavor carries none of it (it has no `embed/`). A Mac install from
a git tree takes the ready launcher from the embed (`deploy/mac/app.py shipped`).

### A module from disk and developer mode (Module API 2.3)

`hub module add <folder> [--dev]` puts a module folder (or a folder of module folders, for example a working copy of the
repo) into `modules.paths` and enables the modules; the code runs straight from it, with no pinned commit and no
`--accept`. `--dev` or `hub module dev <name> [--off]` turns on developer mode (`"modules": {"dev": ["notes"]}`), only for
modules from a folder and an embed's modules (below; not the core's built-in ones and not ones from git):

- editing a file in the module folder restarts the process by itself (about 1-2 s after the save); `module.json` is
  reread; if it fails to start, it tries again after the next edit. Hidden files and folders, `__pycache__`,
  `node_modules`, `venv`, `*.log`, `*.db`, `*.sqlite` and, in a git working copy, everything its `.gitignore` ignores are
  not edits; anything else the module writes into its own folder would restart it, so keep data outside the module
  folder. The first 5000 files of the folder are watched;
- the call budget is 60 s (for the debugger; except `hello`, while `fold` and `actions` keep the core's pass budget);
  failures are recorded but do not suspend the module and do not send you notifications;
- the process output goes to `<state folder>/logs/mod-<name>.log` (the last run); the crash reason (the last log lines)
  and the last failure are shown in `hub modules`. The hub trims a log larger than 4 MB on the fly (2.6): the previous
  content goes to `mod-<name>.log.1` (a single piece, replaced by the next trim) and the process keeps writing from the
  start of the log, with no restart. Before 2.6 the log of a single run grew without limit.

```json
"modules": {"paths": ["~/hub-modules"], "enabled": ["hello"]}
```

`paths` is a module folder or a folder of folders; only a module named in `enabled` is loaded. An example is in
`examples/module-hello`.

`module.json`:

```json
{"name": "hello", "version": "1.0.0", "api": "^2.0", "run": ["python3", "main.py"],
 "description": "what it does", "capabilities": [], "requires": {"modules": {"base": "^1.0"}, "plugins": ["hub-base"]},
 "optional": {"modules": {"extra": ">=1"}}, "budget": {"hello": 1}}
```

- `api` is a Module API range with a lower bound; this hub does not load modules of another line (`^1.x`) and says why.
  The lower bound is the version the module was written for: points, fields and rights newer than it are not given to the
  module (it is refused with a hint about which version to declare). What each version added: 2.0 is the process, `hello`,
  facts, `hub modules`; 2.1 is endpoints and views on the page (`http`), own state and a page plugin (`web`), session
  prompts (`prompt`); 2.2 is `core.write`; 2.3 is `hubmod` with decorators, a module from disk and developer mode, trusted
  sources, a view that calls its own endpoints, and 256 KB of own state (no new points or fields: a module that relies on
  this needs `^2.3`); 2.4 is own `/api/…` paths and snapshot keys for a client with a different API (`http.paths`,
  `http.views`, `snapshot`), and audio transcription by the hub's provider (`voice.transcribe`); 2.5 is `owner.notify`
  with `id` and resolving (above); 2.6 is session-row fields in the slice (the `sessions` right), the `accounts` and
  `messages` rights (below, "Hub sessions"), `hubmod.state` returning and taking a copy, `hubmod.owner` with a malformed
  `dedup_key` answering 400, the repeat code `duplicate`, `hubmod.fact` with `dedup_key` and `wait`, `hubmod.servable` with
  a list and `servable_state()`, and a dev module log capped at 4 MB (no new points); 2.7 is the `model` right and
  `hubmod.model` (below, "Model through the core"); 2.8 is the `profile` right, the points `profile.list`,
  `profile.decide`, `profile.token`, `profile.use` (below, "Session sign-in"), the `speech` right and the point
  `speech.say` (below, "Reply voice"), the `stats` right, `mode` in `hello` and in calls, `ready` in `profile.list` (below,
  "Who runs a function"), and `python3` in `run` meaning the hub's Python; 2.9 is the `bus.transport` right (below); 2.10
  (an experiment, the Codex host) is the points `codex.list`, `codex.decide`, `codex.use`, `codex.add`,
  `codex.remove` and optional points (below, "Codex accounts"), `hubmod.model` without the sessions' model remap
  (below, "Model through the core"), a module without its own process (no `run`, below) and the `task.admit` right with the
  point `task.admit` (below, "Admitting an executor's launch") and the `message.rush` right (below, "Urgent messages").
  Each section below states the
  version it applies from.
- `run` is the process command, started in the module folder. A first word of `python3` or `python` means the hub's own
  Python on any OS (on Windows `python3` is often a store stub, and on a server it may be missing); the rest is used as
  written (for a system Python, use a full path or `./python3`). **No `run` at all (2.10)** is a module without its own
  process: it only gives the page a plugin (`"web"`, below), so the core starts nothing for it and serves its files
  itself (no interpreter, about 25 MB of memory less per module). `hub modules` shows it as "no process", and switching
  it off, removing it and `requires` work as for any module. Whatever needs a process is refused with the reason:
  `capabilities` (an empty list is fine), `commands`, `http`, `fold`, `fold_state`, `actions`, `snapshot`, `budget`,
  `env`; `web` is required, and `api` must be `^2.10` or higher. A hub before 2.10 refuses such a manifest with a
  message about `run` (the module is `blocked`, the hub keeps working). A wrong `run` (an empty list, a string) is still
  an error, not "no process".
- `requires` lists other modules with version ranges and plugins from the catalog (`hub tools list`); `optional` affects
  only the load order. The order is dependencies first, then by name when equal. A missing dependency, a wrong version
  or a cycle makes the module `blocked` with a reason, along with every module that needs it; the rest of the hub works.
- `capabilities` are the rights shown to you before enabling; a module can declare a point that needs a right only if it
  has that right.
- `budget` is the module's own point budget, only lower than the core's (2 s by default).
- An unknown field is a refusal: a module that relies on something this hub cannot do does not load silently.

**Process.** `hub-up` keeps a part `mod:<name>`: the environment has `HUB_MODULE_PORT` (listen on 127.0.0.1 only),
`HUB_MODULE_SECRET` (new on every start), `HUB_MODULE`, `HUB_STATE_DIR`, `PYTHONPATH` (the hub code); the rest is the
service's environment without Claude sign-in keys (below, "Model through the core"). If it crashes it is started again
after a pause; `hub restart mod:<name>` starts it afresh.

**Call.** The core sends `POST /hook/<point>` with JSON and an `X-Hub-Secret` header; a request without the correct secret
is not from the core, so answer 403. The response is `200` and JSON up to 1 MB. The core calls `hello` first
(`{module, api, points}`) and the module answers `{name, version, points}`: the name and version as in `module.json`, and
`points` are the points it answers. The `points` in the request are the points the hub calls on a module of this version
(not newer than the lower bound of its `api`); declaring others is a `hello` failure. After that the module is ready
(`module.ready`); before that it is not called.

| Point | Budget | What the module answers |
|---|---|---|
| `hello` | 2 s | `{name, version, points: [...]}` |
| `fold` | 1 s | a decision on an event before it is recorded (the `core.write` right, below) |
| `actions` | 1 s | an edit of this entry's actions (the `core.write` right, below) |

**Failures.** No answer within the budget, a non-200 response, non-JSON, a response that does not match the schema, or a
process that does not accept connections is a failure (`module.error` in the journal). A module can write `module.error`
only about itself (the spool rejects it for another module, and from a plugin process). 3 failures in 5 minutes mean a
10-minute pause, and a fifth pause in a row (each starting less than an hour after the previous one ended) stops the module
until `hub modules reset <name>`; you get one message at the first pause and one at the stop. If the process exits without becoming ready, or does not answer `hello`, the module is
`blocked` at once (and so are its dependents); the next attempt comes when the hub restarts, on `hub restart mod:<name>`
or on `hub modules reset <name>`.

**Writing** is done with facts into the core's spool, like an extension: your own `mod.<name>.<what>`. The `hub` command
from a module process speaks on behalf of the module (labelled as that module), not as the owner's word: it cannot answer
session questions and permissions, or stop and switch the hub.

`hub modules` shows the modules, their state (ready, starting, paused, stopped, blocked, disabled, and "no process" for a
module without `run`) and the reason;
`hub modules --json` gives the same as data.

### Points 2.0: `migrate`, `launch.env`, `cli`, `doctor`

```json
{"name": "hello", "version": "1.0.0", "api": "^2.0", "run": ["python3", "main.py"],
 "capabilities": ["launch.env", "cli", "doctor"], "env": ["MY_VAR"],
 "commands": {"hello": "say hello: hub hello [whom]"}}
```

The helper for Python is `core/hubmod.py` (above, "Getting started"); the older form without decorators also works:

```python
from core import hubmod
hubmod.serve({"doctor": lambda req: [{"name": "database", "ok": True, "detail": "present"}]})
```

`hubmod.serve` answers `hello` by itself (the name and version come from module.json, the points are the dict keys) and
checks the secret; `hubmod.fact(type, data)` puts a fact into the core's spool on behalf of the module (`channel: "plugin"`, `sender: "mod:<name>"`; the core replaces a module fact's own `channel`, `owner` and `sender` with these, except
for the owner's word below); `hubmod.settings()` is the hub.json section named after the module. A point the module
declares requires the right in `capabilities`; a point's budget can be made lower in `module.json`: `"budget": {"launch.env": 1}`. A failure (no answer within the budget, a response that does not match the schema, a crashed process)
is a `module.error` event; three failures in 5 minutes suspend the module for 10 minutes.

| Point | Budget | The core sends | The module answers | On failure the core |
|---|---|---|---|---|
| `hello` | 2 s | `{module, api, points}` | `{name, version, points}` | did not come up: blocked |
| `migrate` | 10 s | `{from, to}` (the version changed since the last start) | `{}` or `{error}` | blocked, the version stays the old one, retried on the next load |
| `launch.env` | 5 s | `{session, cwd, kind, model, account, agent, resume}` | `{env, args}` | starts as without the module |
| `cli` | 10 s | `{command, args, caller, cwd}` | `{out, err, code}` | `hub <command>` exits with code 1 |
| `doctor` | 2 s | `{api}` | `[{name, ok, detail}]` | a "did not answer" line |

**`launch.env`** applies on every session process start. `env` holds variables: `ANTHROPIC_*`, `CLAUDE_*`, the module's own
`HUB_MOD_<NAME>_*` and the names from the manifest's `env` field; the rest are dropped. Variables that change which code
runs (`PATH`, `LD_*`, `NODE_OPTIONS`, `CLAUDE_CONFIG_DIR`, other `HUB_*`…), the session's network (proxies, trusted CAs,
TLS verification, client certificates: names containing `PROXY`, `CA`, `CERT`, `CERTS`, `TLS`, `SSL`, plus
`SSLKEYLOGFILE`, `CURL_HOME`, `WGETRC`, `DOCKER_HOST`) and the provider choice (`CLAUDE_CODE_USE_*`, except
`CLAUDE_CODE_USE_POWERSHELL_TOOL` and `CLAUDE_CODE_USE_NATIVE_FILE_SEARCH`) are closed here as well as in the manifest. An
empty string removes a variable. `args` holds up to 32 long claude flags and their values, a flag first; they go after the
core's flags. Flags that change settings, MCP, plugins, permissions, the system prompt, the conversation or the protocol
(`--settings`, `--mcp-config`, `--resume`, `--output-format`…) are a failure. Several modules apply in load order, a later
one overriding an earlier one. **The sign-in rule:** if a module set a name that sends claude to another address and it
remained in the environment (`ANTHROPIC_BASE_URL`, any `ANTHROPIC_*` or `CLAUDE_*` containing `URL`, `URI`, `HOST`,
`HOSTS`, `ENDPOINT`, `REGION`, `RESOURCE`, `SOCKET`: provider addresses, a socket, a sign-in address), or, if the session
has a provider enabled (Bedrock, Vertex…), any name from the cloud families `AWS_*`, `GOOGLE_*`, `GCLOUD_*`, `CLOUDSDK_*`,
`AZURE_*`, `CLOUD_ML_*`, `VERTEX_*`, the core removes all sign-in keys (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, other `ANTHROPIC_*`/`CLAUDE_*` secrets: its own, the account pool's, inherited ones), the
`ANTHROPIC_CUSTOM_HEADERS` headers and the provider mode (`CLAUDE_CODE_USE_BEDROCK`…), and keeps only what this module
set. Without its own key (headers alone are not enough) it is a failure and the module's addition is not applied (otherwise
claude would carry the owner's sign-in or cloud key to a foreign address). Two modules that redirect claude under
different names are a failure for the later one. Such a session does not count as a session of a pool account.

A session start waits for modules with the `launch.env` or `prompt` right until they answer `hello` (after a hub restart
the module processes come up later than the sessions), for no longer than 22 s. A module that is still not ready is not
waited for further: the session comes up without it, the module gets a `module.error` of kind `late` (it does not count
toward a pause), and until its process is started anew session starts no longer wait for it. A stopped, suspended or
non-starting module is not waited for.

**A mandatory sign-in module for a session** (optional, in `hub.json`): `"session_providers": {"<session>": "<module>"}`.
The core brings such a session up only if, after the sign-in rule, claude's address is set by exactly this module with its
own key. If the module is missing, did not answer in time, had its addition rejected by the sign-in rule, or had the
address overridden by another module, the start does not happen (`host.error`, and the core tries to bring it up again)
instead of silently falling back to the owner's sign-in or the account pool. A value that is not a string, or is empty,
is also a refusal to start that session; a section that is not an object is treated as no setting (a log line, like other
hub.json sections). The tmux host does not call sign-in modules: it does not bring up the chosen session. Other sessions
behave as without the setting. The setting installs nothing and grants the module no rights.

**`cli`** serves the commands from the `commands` field (the `cli` right): `hub <command> …` calls the module if the
core has no such command. `caller` is `{"owner": true}`, `{"session": "<name>"}`, `{"plugin": "<name>"}` or `{"module":
"<name>"}`. Since 2.10 a schedule job's command or check adds `"schedule_job": "<id>"` to the owner or session caller
(not to the plugin and module callers; on an older hub the field is absent, a module that cannot work without it
declares `^2.10`): a command job runs as the owner without a session, so a module that keeps something for a session (an
owner's reply goes to it) can look the job up in the hub's schedule and keep the item for the session that owns it
rather than for nobody. Like the rest of `caller`, it is what the `hub` command process says about itself (from its
environment), not a proof, and gives no right beyond the owner caller's. A module cannot override a core command; the
core does not choose between two modules offering the same command and refuses, naming both. Module commands are at the
end of `hub --help`. If a command needs stdin (2.8: a secret or token, passed not as an argument so it does not stay in
the terminal history), answer `{"stdin": true}`: the core reads stdin (up to 64 KB) and calls you again with the same
request plus the `stdin` field; without such an answer stdin is
not read. A second `{"stdin": true}` in a row is a refusal of the command.

**`doctor`** is `hub doctor`: settings, claude, the core, each module (disabled, blocked with a reason, no program from
`run`, suspended, ready; a module without `run` is ok) and the modules' checks; the exit code is 1 if anything is `fail`.

## Core modules from git: `hub module add|update|trust|remove`

A core module (a folder with `module.json` and its own process, Module API) can be installed straight from a git
repository:

```
hub module add <git-url> [--ref branch|tag|commit] [--subdir folder]
hub module update [<name>|<url>] [--accept]
hub module trust <name>|<url> [--off]
hub module remove <name>|<url>
```

- `add` fetches from the repo only the needed ref (the source's default branch, a branch, a tag or a commit) and a single
  commit of it, not the whole repo with its other branches and tags. It pins the commit (`modules.sources` in hub.json:
  `git`, `ref`, `subdir`, `commit`, `pinned`; a branch moves, a tag and a commit do not), shows the rights, dependencies and
  start program, and enables the modules it finds (`modules.enabled`). The repo contains one module at the root or in
  `--subdir`, or a folder of module folders.
- The module code runs from its commit's folder: `<state>/modules/<repo>-<hash>/<commit>/` (`git archive`). `update`
  prepares a new folder, and the hub restarts the module process in it; a running process reads only its own files until
  it is restarted. The module keeps its own data outside this folder, because the folder changes on update. Editing the
  commit's files in the folder by hand blocks the module ("files modified"), and `hub module update` restores them.
- `update` applies a new commit only if it does not ask for more than what was approved: new rights (`capabilities`),
  modules and plugins (`requires`, `optional`), changed version ranges, new `env` and `provides` names, a different
  program (`run`), new manifest fields, new modules in the repo. Otherwise it shows exactly what and waits for `--accept`;
  until then the module keeps running on the previous commit. The source is not downloaded again: the module runs from its
  own folder.
- `trust` marks a source as trusted (`"trusted": true` on the source in hub.json; 2.3): `update` applies its new commits
  without `--accept` and prints what they ask for beyond what was approved. `--off` returns it to the usual behavior. A
  local folder does not need to be pinned: `hub module add <folder>` (above).
- `remove` removes a module: the source (the git repo with its `subdir`) or the folder (`modules.paths`) from hub.json, and
  the name from `modules.enabled`, `dev`, `model` and `core_write` (the next module with this name does not inherit the
  consents). The source's downloaded files are removed from disk; a folder in which a process still runs is removed by the
  next `hub module` command, and the hub stops the process. A repo address removes all its sources at once. Files of a
  disk folder and built-in modules are not deleted.
- All `hub module` commands are run by the owner (a command outside a session, not a plugin or module process) or by the
  hub on the owner's word: `--for mN` is the owner's message to the hub, no older than a day (or `qN`, the owner's answer
  to a card); for `remove` the hub may cite his message to any session, no older than a day. Git uses the machine's keys, with no password prompts: for a private repo, an SSH key or a git credential
  helper.

## Core module endpoints on the page: Module API 2.1

A core module (its own process, `module.json`) can answer client requests on the page's port: its own paths, not core
paths. A new core endpoint will not break a module, and a module cannot replace a core endpoint.

```json
{"name": "notes", "version": "1.0.0", "api": "^2.1", "run": ["python3", "main.py"], "capabilities": ["http"],
 "http": {"max_body": 104857600, "max_stream": 3600}}
```

- **Paths.** `/api/mod/<name>/…` accepts any method (GET, POST, PUT, PATCH, DELETE), only with the `X-Hub: 1` header;
  `/mod/<name>/view/…` is an HTML view, GET only, without `X-Hub` (it is opened in an `<iframe>`). Access is the same as for
  the page: localhost, or you from the tailnet. The `http` right in `capabilities` is required; a module that is not ready
  or is suspended gets 503.
- **What the module receives.** The request as is (method, path with `/api/mod/<name>/` and the query, the client's
  headers) over HTTP/1.0 to its port, with `X-Hub-Secret` (check it: every process on the machine can see the port) and
  `Connection: close`. For `/api/mod/…` there is also `X-Hub-Owner: 1`, meaning the request passed the owner check. The
  module never receives the owner's sign-in to the page: no `X-Hub-*` headers from the client, no `Authorization`, no
  `Cf-Access-*` headers, no cookies named `hub_<…>` or `CF_<…>` (Cloudflare Access's: `CF_Authorization`, …). Its
  answer cannot set or clear them either: such a `Set-Cookie` (and a nameless one), `Clear-Site-Data` and
  `WWW-Authenticate` are dropped. A module that needs its own credential names its cookie or header otherwise.
- **The body** is accepted only with `Content-Length` (otherwise 411), at most `http.max_body` (20 MB by default, limit
  1 GB; larger gets 413); it is streamed to the module, and the page does not buffer it. Up to 4 requests with a body at a
  time per module (429).
- **The response** is any status, headers and body, also streamed. With `Content-Length` the client's connection stays open
  for further requests; without it the end of the response is the connection closing. An event stream is
  `text/event-stream`, up to 8 per module; with no byte for 60 s in either direction the connection is closed (send a
  `: ping\n\n` ping), and the lifetime of one response is `http.max_stream` (10 min by default, limit 24 h). If the client
  leaves, the connection to the module is closed. At most 32 connections to modules at once overall (503).
- **A module failure** before the response headers (not listening, silent for 60 s, a non-HTTP response) gives the client
  a 502 with a reason and counts as a module failure, as for core calls: three in 5 minutes suspend the module. Its own
  error response (500, 404) is not a failure.
- **An HTML view** is served with `Content-Security-Policy: sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:; frame-ancestors 'self'`: scripts and styles only inside the document, images from `data:`/`blob:`, fonts from `data:` (2.4). The view
  does not read the hub API, the page or its storage, and does not fetch from the network, even if the embedder forgot
  the `sandbox` attribute. A link with `target="_blank"` (or `window.open`) opens a new tab, and that tab is a normal
  page outside the sandbox — for external links in a report or a plan. Embed it as
  `<iframe sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" src="/mod/<name>/view/…">` (without the
  popup flags links into a new tab do nothing); the view talks to its parent through `postMessage`.
- **The view calls its own endpoints** (2.3). When the hub page loads a frame with `/mod/<name>/view/…`, it gives the
  frame a port (`MessageChannel`) and relays requests over it only to this module's `/api/mod/<name>/…`: another module,
  the hub API, an address outside the module's endpoints, and methods other than GET, POST, PUT, PATCH, DELETE are
  refused. In the view's HTML include `hubmod.VIEW_JS` (or speak the same protocol yourself: `{hub: "port"}` with the
  port; a request `{id, path, method, body, type}`, a response `{id, status, type, body}` or `{id, error}`):
  `await hub.call("/items?x=1", {method: "POST", body: "…", type: "text/plain"})` → `{status, type, body}`,
  `await hub.json("/items", {json: {…}})` → parsed JSON, `hub.go("/details")` → another page of its own view
  (`/mod/<name>/view/details`) or a reload, with a new port. The path is relative to the root of the module's endpoints.
  The module receives such a request with `X-Hub-View: 1` and **without** `X-Hub-Owner` (the owner's word never comes from a
  view). The response is delivered whole; the view cannot get streams (`text/event-stream`) this way. The port belongs to
  one load of the frame: if the view navigates elsewhere by itself (a link, `location`, `location.reload()`), the port is
  gone, and a foreign document in the frame does not get the endpoints; a new `src`, a frame inserted into the page again,
  and `hub.go` give a port again.
- **The owner's word.** Only in response to a request with `X-Hub-Owner: 1` can a module put a fact into the core's spool
  on your behalf: what the page writes (`message.received`, answers to questions and permissions, commands to sessions). In
  Python use `hubmod.owner(req, type, entity, data, dedup_key=None)`: it checks `req.owner` (from a view, with
  `X-Hub-View`, or without a request it raises `HTTPError(403)` and the fact does not go out), sets `"channel": "mod:<name>"`, `"owner": true` and the repeat key (`dedup_key`, below). In another language do the same by hand:
  `core.spool.put(<state>/spool, type, entity, {…, "channel": "mod:<name>", "owner": true}, src="module")`. Never from a
  view (`/mod/<name>/view/…`): any link can open it.

- **Hub sessions (2.4).** For a module with the `sessions` right in `capabilities`, hubd writes the hub's sessions into its
  state slice (`modrun/<name>.state.json`): `"sessions": {name: {"state", "parked", "asleep", "busy", "model"}}` and
  `"renamed": {old name: current name}`. `state` is as in the core (`idle`, `busy`, `starting`, `asleep`…); `parked` means
  parked (no process, a message will bring it up; free as far as you are concerned); `asleep` means the core will accept a
  `wake` (also for a parked one) on the owner's word (`hubmod.owner`; in the module's own name the core refuses to wake,
  put to sleep, take or give back a session, as for any other session); `busy` means a turn is running or the process is
  starting; `model` is the id of the model it was started with. Deleted sessions are not included; there are no texts,
  paths, conversation ids or keys. hubd rewrites
  the slice after an event that changed the sessions, so it can lag the journal for a moment; the core decides regardless (a
  refusal of a command comes through `wait`, above). In Python use `hubmod.sessions()` and `hubmod.session(name)` (also by
  the old name: the record has a `name` field with the current one; if there is no such session, `None`); without the
  right it raises `PermissionError`. In another language, read the same file.

  Since 2.6 a session row has more fields (always present: a module recognizes a 2.6 hub by a key, `"forms" in hubmod.session(x)`; no `api` bump is needed):
  - `retired`: it handed in its task and sleeps, and messages do not wake it;
  - `owner_msg`: `{"id": "mN", "ts"}` of your latest message to it among those held in the hub state (the core removes
    delivered and failed ones older than a day), or `null`;
  - `asks`: your open cards `[{"id": "qN", "since", "until"}]` (`until` is when the card becomes stale: 24 h from `since`,
    and for one asked while you were away (`hub away`), 24 h from your return; one that waits for your return has `null`
    and does not go stale. The slice does not drop cards that went stale by the clock: it is written on an event, so check
    against the clock yourself);
  - `forms`: open forms and permissions `[{"id": <request_id>, "tool", "since"}]`;
  - `reacts`: `{feed row: emoji}` of your reactions that the session has not received yet;
  - `hub_wait`: a `hub wait` check is in progress for it;
  - `limit_cut`: `{"ts", "resets"}` if a turn was cut off by an account limit and the core has not resumed it yet, or `null`.
    `resets` is when the window that refused the turn resets (a time like `ts`), or `null` if the core does not know it
    (the limits before the refusal did not name the window, or its reset was not ahead; nothing to wait for by the clock).
    The row is there until the core continues the turn, not until `resets`.
    The core resumes it itself: with an account pool when the pool moves to another account or the window resets, without one
    when the window resets (the journal fact `session.limit_reset`, the session gets a core message and continues).

  Since 2.10 a row also has (always present on a 2.10 hub, `"worktree" in hubmod.session(x)` tells it; no `api` bump is needed):
  - `raised_by`: the name of the session that raised this one (an executor), or `null`;
  - `since`: when its current state began, a time like `ts` (for one that handed in, `retired`, when it handed in), or
    `null` if the core does not know;
  - `worktree`: `{"branch", "base"}` of its own working copy (each a name or `null`), or `null` for a session that has none.
    The copy's folder and the repository are not in it.

  No texts (messages, questions, options, quotes), no paths, no conversation ids.

- **Claude accounts (2.6).** For a module with the `accounts` right (`api` `^2.6`), the slice carries `"accounts": {"active", "blocked": {name: until}, "exhausted": {"until"} (all are busy until that time) or null, "seen": {name: {"status", "type", "windows": {"five_hour"|"seven_day"…: {"used", "resets"}}, "ts"}}}`. This is what you see in Settings →
  Accounts: names (email) and limits, with no tokens. With both the `sessions` and `accounts` rights the session row also
  carries `account` (which account it runs on) and `limit_cut.account`. In Python use `hubmod.accounts()`.
- **The owner's messages (2.6).** For a module with the `messages` right (`api` `^2.6`), the slice carries
  `"owner_messages": {"mN": {"to", "ts", "answers", "form"}}`: all your messages held in the hub state. `to` is the
  recipient (as the core understood it; a rename carries over), `ts` is when, `answers` is the card `qN` the core counted
  this as an answer to, and `form` is the `request_id` of the form you answered (or `null`). As in the core: "Deny" on a
  card is your message but its `answers` is `null`; closing a card with × is a core note to the session and is not listed
  here. No text. In Python use `hubmod.owner_message("mN")` → a record or `None` (not yet recorded or already removed).
- Without the right the part is absent from the file and `hubmod` raises `PermissionError`. If there is no slice file
  (hubd has not written it yet, or the write failed and the next tick writes again): `sessions()` reads the whole hub state
  as in 2.4, while `accounts()` and `owner_message()` return `None` ("I don't know", ask later) and do not read the whole
  state. Each part is built once per event for all modules, and only if someone has its right; the file is rewritten only
  if its parts changed.

### A module for a client with a different API: own paths and snapshot keys (Module API 2.4)

A client written for a different hub (an app, a script) uses its own paths (`/api/files`, `/api/session/rename`) and learns
the capabilities from the snapshot's keys and `features`, not from `/api/mod/<name>/…` and `mods`. A module can answer it
there without touching the core:

```json
{"name": "client-api", "version": "1.0.0", "api": "^2.4", "run": ["python3", "main.py"], "capabilities": ["http"],
 "http": {"paths": ["/api/notes"], "views": ["/api/notes-view"]},
 "snapshot": {"keys": ["notes"], "features": ["notes"]}}
```

- **`http.paths`** are endpoints on its own paths: the page hands the module a request for `/api/notes` the same way as for
  `/api/mod/<name>/…` (access, `X-Hub: 1` on all methods, `X-Hub-Owner`, body and stream limits: as above). The module sees
  the path as `/api/mod/<name>/api/notes` (query as is), so in `hubmod` it is an ordinary `@hubmod.route("/api/notes")`.
- **`http.views`** are HTML views on its own paths: GET only, no `X-Hub`, no `X-Hub-Owner`, in the same CSP sandbox as
  `/mod/<name>/view/…`; the module sees `/mod/<name>/view/api/notes-view`, handled by `@hubmod.view("/api/notes-view")`.
  This is a view for a foreign client: the hub page gives the port to its own endpoints (`hub.call`) only to a frame with
  an address of `/mod/<name>/view/…`, and the same view is opened there as well.
- A path is `/api/` followed by parts made of lowercase Latin letters, digits and `_.-`, with no query and no trailing `/`;
  paths and views together are limited to 32; `/api/mod/…` is not allowed (those are your endpoints anyway). The `http`
  right is required. Matching is on the whole path only.
- **`snapshot.keys`** are top-level snapshot keys (`/api/snapshot`, `/api/stream`): the value is your state key
  `mods.<name>.<key>` (`hubmod.state.set("notes", …)`), up to 32 keys matching `[a-z][a-z0-9_]*`. **`snapshot.features`**
  are names in the snapshot's `features` (up to 32). Keys and features are in the snapshot **only while the module is
  ready**: it answered `hello` in this process run, and is neither suspended nor stopped. A key must also be **written by
  this run** (the value from a previous run is not shown until the first write: rebuild it after start, in `hubmod` with a
  function marked `@hubmod.on_ready`). `hubmod` tags a write with the run itself; in another language use the field
  `"launch": $HUB_MODULE_LAUNCH` on `mod.<name>.set`; rebuild when `modules.<name>.ready` in the hub state becomes your run
  (before the module's first readiness in this hub a write is rejected, and before migration it is overwritten by it). If
  the module crashes, is suspended or is disabled, its keys and features leave the snapshot (the stream sends a patch with
  `_gone`), and the client does not offer what will not answer now. `mods` stays as it was.
- **The core wins.** A path, key or feature that the core has (`/api/send`, `/api/stream`, `sessions`, `voice`, `push`…) is
  not granted to a module, neither now nor when the core introduces the same one in a new version: the module keeps
  working without it. Core paths are those the page discovers itself; a path under a core path (`/api/usage/cycles`) is
  yours.
- **Two modules with the same path or key** both go without it until you choose one: `"modules": {"use": {"paths": {"/api/files": "<module>"}, "keys": {"todo": "<module>"}}}` in `hub.json`. Features do not conflict.
- What a module did not get is shown by `hub modules` (a "not granted: …" line) and a `module.claims {module, lost}` entry in
  the journal at every change.
- Without these fields everything is as before: endpoints at `/api/mod/<name>/…`, state in `mods`. An example is in
  `examples/module-client-api`.

### Audio to text via the hub's provider (Module API 2.4)

A module with voice (its own recording stream, another client) transcribes audio with the same provider as the page's
microphone and the Telegram voice messages, through the `voice` section of the hub settings (`voice.cmd`, `ffmpeg`). The
right is `"voice.transcribe"` in `capabilities`:

```python
import threading
from core import hubmod

stop = threading.Event()   # stop.set() from another thread aborts a transcription in progress
try:
    got = hubmod.transcribe(wav_bytes, "audio/wav", timeout=30, cancel=stop)
    text = "" if got.get("empty") else got["text"]
except hubmod.TranscribeError as e:
    if e.code == "off":
        ...   # voice is not configured on the hub: answer the client 404, and it will transcribe by itself
```

- The result is `{"text": "…"}` or `{"empty": true}` (the provider heard no speech; silent audio too). `hubmod` reads the
  voice settings itself and does not hand them to the module.
- To ask "is voice available?" before recording audio, use `hubmod.voice_available()`: `True`/`False` (voice not
  configured: answer the client 404, and it will transcribe by itself) without calling the provider; with no right, or if
  the settings cannot be read, it raises the same `TranscribeError` (`denied`, `failed`).
- A refusal is `hubmod.TranscribeError`, with a `code` field: `denied` (no right, or the process was not started by the
  hub), `off` (voice not configured), `format` (the type is not one of `audio/webm`, `audio/ogg`, `audio/wav`,
  `audio/mp4`, `audio/mpeg`, `audio/flac`…; type parameters such as `; codecs=opus` do not matter), `timeout`,
  `cancelled`, `failed` (a provider failure; the error text is its reason, for the log, not for parsing).
- `timeout` is the seconds for the whole call and `cancel` is a `threading.Event`: either one stops a call in progress
  together with ffmpeg and the recognition command, rather than merely stopping the wait. Without them the provider's
  limit applies (up to 2 min for ffmpeg and 5 min for the command; when it runs out the result is `failed`).
- The call is synchronous, in the module's process: call it from your own thread; how many calls run at once is up to the
  module.
- The right is a declaration shown to you before enabling, not a sandbox: the module process can read the hub's files
  anyway.

### Model through the core (Module API 2.7)

A module that needs a short model answer (compress a text, parse a phrase, a yes/no question) asks the hub for it instead of
reading the account tokens itself. The right is `"model"` in `capabilities` (`"api": "^2.7"`):

```python
from core import hubmod

try:
    got = hubmod.model("Compress the text into one line.", text=long_text, model="haiku",
                       schema={"type": "object", "required": ["line"], "properties": {"line": {"type": "string"}}},
                       max_tokens=512, timeout=60)
    line = got["json"]["line"]     # without schema, use got["text"]
except hubmod.ModelError as e:
    if e.code == "off":
        ...                         # the owner has not allowed the model yet: work without it
```

- `prompt` is the system prompt and `text` is the input; together with `schema` up to 100,000 characters, and `schema`
  itself up to 6,000 (it goes to `claude` on the command line, the prompt goes in a file and the input through stdin).
  `model` is `haiku` (the default) or `sonnet`: Claude Code aliases, so the latest model of the family is called. Since
  2.10 the call does not follow a model remap the hub gives its sessions (`ANTHROPIC_DEFAULT_*_MODEL`, `ANTHROPIC_MODEL`,
  `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` from the Claude Code settings `env` and hub.json
  `user_env`): `haiku` is haiku; a person's own provider (their own `ANTHROPIC_BASE_URL`, Bedrock, Vertex) keeps it. `schema`
  is the JSON Schema of the answer (the result is then `{"json": …}`), `max_tokens` is the output ceiling, 1...4096 (1024),
  `timeout` is 1...120 seconds (60). The result also has `usd`, the cost at API list prices (or `None`), and `model`, what
  was called.
- The page of the hub makes the call: `claude -p` without tools, settings or MCP, on the current pool account. The token
  never reaches the module; the page recognizes the module by the secret of its run (`HUB_MODULE_SECRET`), and a request
  from another process is refused.
- The owner allows the model for a module: Settings → Modules → "Allow the model", or `hub module model <name> [--off]`
  (`modules.model` in hub.json). Without permission the module works, and the call returns `off`.
- Limits: 2 calls per module at once and 4 across all modules (`busy`, no queue; the module decides whether to wait), and
  60 calls per module per hour (`limit`; the count survives a page restart).
- The spend is shown on the "Usage" page of the ☰ menu (`#/x/usage`) as a "module <name>" row (in `/api/usage` the key is `mod:<name>`; calls without a
  price are `unpriced`). The prompt and answer text are not written to the journal.
- A refusal is `hubmod.ModelError`, with a `code` field: `denied` (no right, or the process was not started by the hub),
  `off`, `bad` (wrong model, longer input, limits), `busy`, `limit`, `unavailable` (the page does not answer; `HUB_NO_MODEL`
  means no model is called in tests), `timeout`, `failed` (account limit, network, sign-in; the text is for the log, not
  for parsing).
- The call is synchronous: call it from your own thread. The page is on the hub's `HUB_WEB_PORT` port (8787 by default).
- The right is a declaration for the owner, not a sandbox: the module process can read the hub's files. This is the
  legitimate path to the model; a module that reads the accounts file itself does not become legitimate by it, and
  stopping that is a change in the module itself.
- The Claude sign-in keys (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_*`/`CLAUDE_*` secrets) and the provider mode (`CLAUDE_CODE_USE_BEDROCK`…) are
  absent from the module process's environment, even if the hub service has them. This closes an accidental leak through
  the environment; it is not protection against a module of the same user, which can still read the sign-in files.

### Session sign-in: a sign-in module (Module API 2.8)

A sign-in provider decides which Claude Code sign-in the sessions and the hub's own `claude -p` calls (the watchdog, the
critic, `hubmod.model`) run on, and when to switch to another. By default this is the hub's built-in account pool
(`hub accounts`) on a hub that has one, and the computer's own Claude Code sign-in on a hub without it.
A module with the `"profile"` right (`"api": "^2.8"`) can take this over: at once, if the owner named it in hub.json
(`"modules": {"use": {"profile": "<name>"}}`; `"builtin"` keeps the hub's pool), or by itself after a shadow period next to
the hub's pool (below, "Who runs a function"). The sign-in state (the active account, busy ones, limits, session switches
with the same conversation, resuming a turn cut off by a limit) is still run and written by the core: the module answers
with what to record, and the core writes the `account.*` events.

| Point | Budget | Request | Response | No response |
|---|---|---|---|---|
| `profile.list` | 2 s | `{}` | `{on, switch_at, profiles: [{name, email?, login?, renewable?, paused?}], paused?, route?: {on, ident, why?, code?}}`, with no tokens or secrets; `route` is the module's own gateway (below) | there is no Accounts tab (`accounts: null` in the snapshot), economy mode works as without a pool, and the gateway state does not change |
| `profile.decide` | 2 s | `{account, limits, now}`: the sign-in state without token fingerprints | `{}`; `{switch_to, reason, restart?, refresh?, fresh?, free?, reset?, blocked?}`; `{exhausted: true, until, blocked, reason}`; `{waiting: true, until, account, reason}`; and `gens: {profile: token generation}`, `renewed: {profile: fingerprint}` (optional: a busy profile that is not the active one took another token by command, so the core lifts its block and old measurement — the same `account.token` write with `fresh`, then asks again; a core before this change ignores it). The core writes a new generation of a known profile first and immediately asks again with the new state: the decision is taken from the second answer (if there is none, there is no decision until the next question) | nothing changes |
| `profile.token` | 5 s | `{profile, session?, kind: session \| model}` | `{env, gateway?: {port, secret, first_party?}}`. `env` holds sign-in keys only (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_*`/`CLAUDE_*` secrets), and an empty value removes the variable; `gateway` goes through the module's own gateway (below); `{}` means the profile has no sign-in (there is no such profile, or no token yet): the session runs on the machine's sign-in without `HUB_ACCOUNT`, and the core's `claude -p` skips the call. `{"env": {}}` is different: the machine's sign-in, labelled with this profile | the machine's Claude Code sign-in, and a log line |
| `profile.use` | 2 s | `{name, force, account, now}` | `{ok: true}` or `{error: unknown \| paused \| busy, why?, until?, forceable?}` | `/api/accounts/use` returns 503, `hub accounts use` exits with code 1 |

- The core calls `profile.decide` on a tick when the sign-in changes (the active account, limits, turns cut off by a limit)
  and at least once every 30 s; `switch_to: null` means back to the machine's sign-in. The core does not write a repeat of
  what is already recorded (the same wait, "all busy" until the same time, a switch to the active account without a new
  token). If the module does not answer, there is neither a switch nor a return to the default sign-in: a module that comes
  up after a hub restart does not pull sessions off their sign-in.
- `profile.token` is called on every session process start; a session start waits for a module with the `profile` right
  until it answers `hello` (as with `launch.env`). The address, network and other environment do not go through this point
  (a schema failure).
- The limits that the module sees itself are the fact `account.limits` (`hubmod.fact("account.limits", {account, status, type, windows, gen?})`): the core accepts it only from the sign-in module and only for a profile from its `profile.list`.
- The module process's environment has no sign-in keys (above): the module keeps the profile tokens itself.
- Its own gateway (switching to another profile without restarting sessions, like the hub's account gateway) is local HTTP
  on `127.0.0.1:<port>` that substitutes the profile's token into every claude request. The module reports it in
  `profile.list` (`route: {on, ident}`; if it is not working, `{on: false, why, code?}`), and in `profile.token` as the
  location `gateway: {port, secret}` together with the direct token in `env` (the core brings a session up with it if the
  core's gateway is off). `ident` is the first 12 characters of sha256 of `"<port>:<secret>"`; if it does not match the
  location from `profile.token`, the session is brought up again. The core builds the session's environment for the gateway
  as for its own gateway: the address `http://127.0.0.1:<port>`, the header `x-hub-gateway: <secret>` (the gateway checks it
  and does not forward it), the placeholder token `hub-gateway` instead of the profile's token, and `127.0.0.1` in
  `NO_PROXY`; `first_party` means the gateway forwards byte for byte to the Anthropic API (the flag
  `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL`). The secret matches `[A-Za-z0-9_-]{16,256}`. The owner turns the gateway on and
  off with the same setting as the hub's gateway: `gateway.enabled` in hub.json (the Accounts page,
  `hub accounts gateway on|off`), which the module reads itself. For that time the hub's gateway takes no new sessions and
  keeps serving existing ones until they restart; for them to carry on without a restart, watch its location, the port and
  secret from `<hub folder>/gateway/gateway.json`: as soon as `route` names that location, the hub's gateway frees the port
  (take it as soon as it is free). A module that no longer runs the sign-in (`modrun/providers.json`, `mode` is not
  `live`) frees the port: the hub's gateway waits for it for up to a minute, then moves to another port (sessions on the
  gateway restart with the same conversation).

- Every `profile.*` call carries `mode` (`hello` too, for the moment of launch: the strictest of the module's functions and
  of `modes` for each): `live` means the module runs the sign-in; `shadow` means the hub's pool runs it and the module only
  reads its own pool and answers: it does not run token commands, does not touch the keychain, does not write pool files,
  does not bring up a gateway and does not send `account.limits` (the core will not accept them in shadow); `warm` means
  tokens by command and the conduit are allowed, while decisions and the gateway are still with the hub.
- `profile.list` may carry `ready: {tokens, conduit, gateway}` with the values `ok | fail | off | wait`: whether the module
  is ready to take over (fresh tokens for all profiles with a command have been obtained; the conduit works or is off; the
  gateway can serve or is not needed). The transition out of `warm` waits for it.

### Who runs a function: built-in, then shadow, then module (Module API 2.8)

The core can run session sign-in (`profile`), reply voice (`speech`) and the hub's statistics report (`stats`) by itself. A
module with the matching right takes a function over like this (who runs it now is in the last lines of `hub modules`):

- **The owner named a module** (`modules.use.<right> = "<name>"`): the module runs it at once. If it is paused after
  failures, stopped or disabled, the core runs the function; when it returns, the module runs it again. `"builtin"` means
  always the core.
- **The owner did not name one**: the candidate is the only ready module with this right (two means neither, until the owner
  chooses), its `doctor` (if it has one) is entirely green, and the hub's built-in code for this function has not been
  edited on top of a release.
  - `profile`: first a **shadow**: the core asks the module the same `profile.decide` questions that the hub's pool decides,
    compares the answer (switch, "all busy", "wait for reset", the deadline, busy accounts, restart, fresh token, the `gens`
    fingerprints) and does not apply it; every 10 minutes it compares `profile.list` and the tokens of profiles with a
    permanent token. A day of observation and dozens of comparisons with no mismatches make it **warm** (`mode: warm`);
    when the module says `ready`, it takes over (`live`), and the hub's gateway yields its location to it. A mismatch is a
    `module.error` with `kind: shadow` (both answers, without secrets), the function goes to the core, and this version of
    the module is no longer a candidate until a new version is released or the owner runs `hub modules reset <name>`.
  - `speech`: takes over at once, except that a module that asks for the `"model"` right (to compress the reply before
    speaking it) does not take over until the owner has allowed it the model (`modules.model`, `hub module model <name>`):
    without it, it would speak the raw text without compression, and this does not count as a failure; the core keeps the
    built-in voice and records the reason, and once the model is allowed the module takes over (this applies to a module
    named by the owner too; revoking the permission returns the built-in voice). Two failed speakings in a row (silence,
    `{error}`) make the hub speak the next replies, and this version of this module is out until a new version or
    `hub modules reset` (a module named by the owner as well); `{}` ("no voice message will come") is not a failure, and
    neither is a module that has not come up yet after a hub restart. There is one voice message per reply: if the module
    does not answer, that reply has no voice, and the hub's voice is not switched on for it.
  - `stats` (the `"stats"` right, no points): takes over at once, and the built-in report goes quiet. The file
    `<hub folder>/stats/state.json` is shared: before sending a period, under the lock `state.json.lock`, a `sending {from, by, at}` mark is written, and afterwards the result: on success a new `from`; on failure `tried` (the time), `fails`
    (consecutive failures of this sender: if the previous `by` was different, the count starts from one) and `by` (who
    sent). Three failures in a row with the module as `by` after the switch, or its `module.error`, make the hub send the
    report again. To learn who runs it now, look at `<hub folder>/modrun/providers.json` before each send (`hello` knows
    only the moment of launch).
- If the module is paused, stopped, disabled, or the built-in code is edited, the core runs the function again; a module
  that has not come up yet after a hub restart keeps running it (session start waits for it).
- **No built-in code in the tree** (the open flavor of the app is built without the hub's pool with its gateway and
  conduit, its voice and its report): without a module nobody runs the function — sessions run on this machine's Claude
  Code sign-in, a reply stays text, no report is sent. A ready module with the right takes the function over at once (no
  shadow and no warm-up: there is nothing to compare it with); a pause after failures and failed speakings or sends keep
  it (there is no other one), stopping, disabling or removing it leaves the function to nobody. `module.provider` of such
  a move carries `bare: true`.

### Reply voice: a voice module (Module API 2.8)

A reply to the owner in Telegram goes out as text, and if "Voice reply" is on (`voice_reply.enabled` in hub.json), a voice
message follows. By default the hub produces it itself (`modules/voice/speak.py`: text compression, Codex realtime, ffmpeg).
A module with the `"speech"` right (`"api": "^2.8"`) can speak instead: one named in hub.json
(`"modules": {"use": {"speech": "<name>"}}`; `"builtin"` means the hub's voice) or the only ready one (above, "Who runs a
function"; for a module with the `model` right, only once the model is allowed). The switch stays the same,
`voice_reply.enabled`: when it is off, the module is not called. The module reads its own settings (voice, model version)
from the `voice_reply` section itself.

| Point | Budget | Request | Response | No response |
|---|---|---|---|---|
| `speech.say` | 150 s | `{text, channel: "telegram", session?}`: the reply text as is, and the session the reply belongs to | `{path, seconds, said}`: an absolute path to a finished OGG/Opus, the seconds (0-600), and what was said; `{error}` means it failed, with the reason as a string; `{}` means no voice message will come | there is no voice message for this reply, the text is already with the owner; the built-in voice does not speak it |

- The hub copies the file at `path` into its own temporary folder and removes the copy after sending; **the hub does not
  touch the module's file**, so the module removes it itself. If it is not OGG (the first bytes are not `OggS`), not a
  regular file, empty, or larger than 20 MB, the voice message does not go out, and the owner sees a "voice message
  failed" line.
- `{error}` is shown to the owner as a quiet line after the text (`telegram.speech_failed`); silence shows nothing.
- Each reply is called in the background, one at a time: the next waits until the module finishes the previous one.

### Codex accounts: a Codex pool module (Module API 2.10, experiment)

On a hub with the Codex host (hub.json `"host": "codex"`, or `HUB_CODEX=1`), several Codex accounts can serve the Codex
sessions, as the Claude account pool serves Claude Code sessions. An account is a `CODEX_HOME` folder: account 1 is the
hub's own `<hub folder>/codex-home`, every other one is a folder inside the module's data folder `<hub folder>/moddata/
<module>/` (a real folder, the hub user's, `0700`). The core prepares such a folder itself (settings, links to the hub
folder's conversations and MCP sign-ins), signs in by a device code, keeps the active account and the limits and moves
sessions: after their turn, and a turn cut by a limit at once. The module (the `"profile"` right) answers which accounts
there are and when to switch; the core writes `codex.pool`, `codex.switched`, `codex.exhausted`, `codex.waiting`.
The pool is not in every build of the hub: a hub without its code (the public build) runs Codex on its one sign-in and
does not offer these points (a module learns it from the `points` of the core's `hello` request, below).

| Point | Budget | Request | Response | No response |
|---|---|---|---|---|
| `codex.list` | 2 s | `{}` | `{on, switch_at, profiles: [{name, home, hub?}]}`; `home` is absolute, `hub: true` is the hub's folder; a folder the core does not accept drops out with a log line | the rows and the active account stay as they are |
| `codex.decide` | 2 s | `{account, limits, now, rows}`: the Codex pool's state; `rows` — the names whose folders the core took (choose among these) | as `profile.decide` without `gens`, `refresh`, `fresh` | nothing changes |
| `codex.use` | 2 s | `{name, force, account, now}` | as `profile.use` | `hub codex use` exits with code 1 |
| `codex.add` | 5 s | `{}` | `{name, home}` of a new empty folder and pool row, or `{error}` | the page says the pool module does not answer |
| `codex.remove` | 2 s | `{name}` | `{ok: true}` or `{error}`; the folder stays (a conversation begun in it lives through it) | `hub codex remove` exits with code 1 |

- The provider is the module whose last `hello` declared `codex.list` (a restart or a pause does not change it);
  `modules.use.profile`, if it declared it, comes first. No module declares it — no pool: sessions run in the hub's folder.
- **Optional points.** The `codex.*` points are optional (`version.OPTIONAL_POINTS`): a module with a lower api floor
  (`"api": "^2.8"`) may declare them, so one module version serves cores with and without them. Declare one only when the
  core offers it: it is in the `points` of the core's `hello` request (`"codex.list" in core.modrun.callable_points()` in
  Python); a core without it refuses an unknown point in `hello`.
- Facts the core takes from the provider about a row of its `codex.list`: `account.limits` with `engine: "codex"` and
  `account`, and `codex.account_lost {account, why}` (a sign-in that no longer works). Run every `codex` of an account
  folder with that folder's `CODEX_HOME` and the hub's arguments (`core.codexcli.Rpc(state_dir, home=…)`).

### Admitting an executor's launch: the `task.admit` right (Module API 2.10)

Before the hub raises an executor for a request (`hub task run …`) it asks the modules with the `"task.admit"` right
(`"api": "^2.10"`) whether to let it start now. A module can keep a request back for its own reason: a deploy window,
a budget, a freeze, a check that must pass first.

| Point | Budget | Request | Response | No response |
|---|---|---|---|---|
| `task.admit` | 2 s | `{run: {name, by, task, repo, branch, here, work, ts, held}}`: the request (`by` is the session that asked, `null` for the owner; `task` is cut to 4000 characters; `held` is true when the module held it on an earlier pass) | `{}` to let it start, or `{hold: "<reason>"}` (a non-empty line, cut to 200 characters) | the request is not held: the executor starts |

- **What a hold does.** The request goes back to the queue (`hub task queue`, the mini-hub rows on the page) with the reason
  as why ("held by the module <name>: <reason>"). It takes no place, the queue does not raise it by itself, and
  `hub task cancel` removes it as any queued request. The hub asks again every couple of seconds, so a module answers
  from its memory (a measurement never runs inside the question); the first module in load order that holds wins. `{}`
  lifts the hold: the request waits for room or starts as any other. `hub task go` raises a request out of turn, and the
  module is asked again before the copy is made.
- **A module never holds by failing.** No answer within the budget, a malformed answer (`{hold: ""}`, extra keys), a
  crashed or suspended or not yet ready module, or a module removed or switched off all let the request through; a module
  that missed is not asked again in the same pass, and the request is asked again on the next one. Three failures in 5 minutes suspend it as any module.
- **Not asked:** a wake (a message to a sleeping executor), a request that waits for room (it is asked when it may rise),
  and a request whose working copy is already being made.
- The module sees the task text of every executor request, so the right is in the list the person sees before enabling
  the module ("read the tasks of executors before they start and hold a launch for a reason"). Only the hub's
  executors are asked: a session you start yourself is not. A pass of questions stops after 5 seconds in total: a
  ready request not yet asked starts on a later tick, never unasked.

### Hub bus transport: the `bus.transport` right (Module API 2.9)

The hub bus (hubs writing to each other; off by default, `hub bus on`) seals and signs every envelope in the core; a
transport only carries bytes. The hub has two transports of its own: the relay (`hub bus relay join <invitation>`, the
`bus-relay` part; the server is `relay/` in this repository, anyone can run one) and a shared folder for a stand
(`hub bus wire <folder>`). A module with the `"bus.transport"` right (`"api": "^2.9"`, no points) can carry envelopes
another way. It gets no keys and sees no text.

- **What to hand**: `<state>/bus/transport.json` — `{enabled, me, relay, allow, out: [{out, fp, id, exp, kind, status,
  handed?, remote?}]}` — and the envelope bytes in `<state>/bus/out/<out>.env`. The core rewrites both when they change;
  read the file, never the state. `fp` is the receiving hub; an envelope is the same bytes on every try.
- **Handed**: `hubmod.fact("bus.handed", {"out": "<out>", "via": "<your transport>"})`. A receipt or an introduction
  leaves the list; a message stays (`status: "handed"`) until its receipt comes or it expires — hand it again now and
  then if your way can lose it.
- **Brought**: write the envelope into `<state>/bus/in/<sha256>.env` (a regular file, at most 128 KiB; write to a
  temporary name and rename), then `hubmod.fact("bus.offered", {"path": …, "sha256": "<hex>", "size": <bytes>})`. The core
  copies and checks it (signature, contact, repeat, limits), decides and removes the file; a file outside that folder is
  refused and never touched. Keep that folder under 200 files and 20 MB.
- A module without the right gets `core.rejected` for both facts; no other fact of the bus is open to a module.

### Pausing the executor queue: the `queue.pause` right (Module API 2.10)

The executor queue (`hub task queue`) raises nobody while it is paused; the hub and the owner pause it with
`hub task pause` and lift it with `hub task resume`. A module with the `"queue.pause"` right (`"api": "^2.10"`, no
points) pauses it with a pause of its own and lifts only that one — a build or a deploy that must not meet a new executor:

- **Pause**: `hubmod.fact("task.queue_paused", {"why": "<a line the owner reads>"})` (at most 200 characters). **Lift**:
  `hubmod.fact("task.queue_resumed", {})`. Repeating either is harmless; lifting when nothing of yours is set is quiet.
- The queue stands while anyone's pause stands: the hub's or the owner's, and each module's. A module's resume lifts
  only its own pause; when the queue is paused by someone else, it is refused (`core.rejected`, with who holds it).
  The owner's or the hub's `hub task resume` lifts all of them, so a stuck module cannot hold the queue.
- The snapshot's `queue_paused` is the pause everyone sees: the hub's or the owner's first, else the oldest module's,
  with `by: "mod:<name>"` for a module (`null` stays the owner). Executors already raised are not touched.
- A module without the right gets `core.rejected`; another outsider (a plugin, a session) is refused as before: the hub or
  the owner pauses the queue.

### Urgent messages: the `message.rush` right (Module API 2.10)

A message to a busy session waits until its turn ends; the hub and the owner can send one urgently (`hub send --now`):
the core types it into the session in the middle of its turn, and Claude Code reads it between steps. A module with the
`"message.rush"` right (`"api": "^2.10"`, no points) can do the same — a relay of an outside event that must not wait
minutes for a long turn:

- **Send**: `hubmod.fact("message.received", {"to": "<session>", "text": "<the message>", "now": True})`. Without `now`
  the message waits for the turn as before. When the session cannot take it mid-turn, an urgent message goes first in
  its queue.
- The core checks the right before the fact is written; a module without it gets `core.rejected`, and so does a plugin.
  The right covers messages only: an urgent task from a module (`hub task new … --now`) is refused as before.
  The core names the sender (`mod:<name>`), so the session sees which module wrote.
- Deciding when an outside event is urgent is the module's job: check its caller (a signature, a token) before you
  pass `now`, so a caller you do not trust cannot interrupt sessions mid-turn.

## Core modules: own state and a page plugin (Module API 2.1)

A core module (Module API, `module.json` with its own process) keeps its own state in the hub journal. The fold is done by
the core's shared code, and the module's code does not take part in it, so the page, the bot and the history see the same
thing, and a journal replay gives the same state. The module writes with facts into the core's spool (`core.spool.put`),
like an extension:

| Fact | What happens |
|---|---|
| `mod.<name>.set` `{key, value, if_rev?}` | key := a JSON value (`null` is a value too) |
| `mod.<name>.del` `{key, if_rev?}` | the key is removed (no key and no `if_rev` means nothing happens) |
| `mod.<name>.<what>` `{…}` | just a journal entry (the module's history: who closed what and how); the state does not change |

- The state is `mods.<name>.<key> = {value, rev}`; `rev` is the number of the write event (`seq`), global: a key that was
  deleted and created again gets a new number. `if_rev` makes the write happen only if the key is still at that revision;
  `0` means only if the key does not exist. Otherwise it is rejected with a reason ("key is at revision N"): this way an
  endpoint, a command and a module process do not overwrite each other's updates. There is no atomicity across several
  facts. The current revisions are in the snapshot (`/api/snapshot`) or via your own journal reader (`core.tail.Tail`).
- Only a module that has answered `hello` at least once in this hub (`module.ready`) can write; facts before the first
  `hello` are rejected as "not loaded". A module's own branch is only `mods.<name>` taken from the event name; `entity` is
  empty or the module name.
- Limits: a key matches `[a-z0-9_.-]{1,64}`; per module 256 keys and 256 KB (keys + values in compact JSON; 32 KB before
  2.3), for all modules 512 KB (128 KB before 2.3); `NaN` and infinity are rejected. The page stream resends the whole
  `mods` on any write, so frequent and large data belongs in a module endpoint or stream, not here.
- **A repeat without a duplicate** is `dedup_key` (a string of up to 128 characters) on a module fact: `mod.<name>.*`, or
  with the owner's word from a module endpoint (`channel: "mod:<name>"`), and since 2.6 on any fact via
  `hubmod.fact(…, dedup_key=…)`. Only an accepted fact occupies the key; a repeat with the same key from the same module
  within 24 hours by journal time is rejected, and the reason contains the number of the accepted one: `mN` if the fact
  created a message, otherwise "event N". This way a client's `client_id` is one fact, with no "wrote the key but did not
  send" window. A module's keys are shared across its `mod.*`, the owner's word and `hubmod.fact`; up to 4096 are
  remembered, beyond which the oldest go sooner than a day. Recognize a repeat by its code, not by the reason text (2.6):
  `core.rejected` carries `code: "duplicate"`, and the reason from `hubmod.owner(…, wait=)` and `hubmod.fact(…, wait=)` is a
  string with a `.code` field (`getattr(why, "code", None) == "duplicate"`; `None` for other rejections); the text is as
  before and may change. For `hubmod.fact` the key is not a `data` field: it goes into the spool file's `dedup` field, and
  the core writes it into the event's `dedup` field (only for a fact whose sender is `mod:<name>`); a `dedup_key` inside
  `data` of an ordinary module fact is, as before, ignored by the core. The core does not know keys accepted before the hub
  was updated to 2.6: when moving to `dedup_key`, the module also checks its own earlier memory for a day.
- A rejection is the next journal line `core.rejected {of, type, reason, code?}`, as for any fact.
- If a module is disabled, its `mods` stay in the journal and state, but leave the snapshot.

**The snapshot** (`/api/snapshot`, `/api/stream`): while at least one module is loaded, it has `mods` (`{module: {key: {value, rev}}}`, loaded modules only), `mod_versions` (`{module: version}`) and `"modules"` in `features`. With no modules
these keys are absent altogether, and the snapshot is as before.

**A page plugin from a module** is the `"web": "web/main.js"` field in `module.json`: the same Plugin API as for the
plugins below. The page loads it from the module folder (`/mod/<name>/web/main.js`; only files from the main file's folder
are served: js, css, svg, json, png), and in the snapshot's `extensions` it is `{name: "mod:<name>", main, base: "/mod/<name>/web/"}`; `hub.url(file)` and `hub.addStyle(file)` use the same folder. The plugin exists while the module is
loaded. A module may have nothing but the plugin: leave out `run` (2.10) and it needs no process.

### Core module: session prompts, `prompt.parts` and `prompt.input` (Module API 2.1)

A module with the `"prompt"` right in `capabilities` declares the point `prompt.parts`, `prompt.input` or both in `hello`.
The core calls them on the stream-json host (the main host), with a budget of 1 s per call; several modules are called in
turn in load order, each receiving the previous one's result. If there is no answer, the answer does not match the schema,
or the process crashed, the module's step is skipped: the parts and the text stay as they were before it.

- **`POST /hook/prompt.parts`** is called on every session process start. Request: `{session, cwd, resume, parts: [{name, text}]}`: the core's system addendum split into parts, in the order the session will receive it. Response: `{parts: [{name, text}]}`, the whole new list: append your own part, replace the text, remove, reorder. Names are non-empty, up to
  64 characters, without repeats; the text length is not limited (only the whole response, 1 MB): the core passes a changed
  addendum to Claude Code as a file. An empty list means no addendum. The parts currently are `agent_file` (the agent's own
  CLAUDE.md, only for agents and their executors, first; for an executor, a digest if the agent has a `CLAUDE.executor.md`,
  see `core/execprofile.py`), `language`, `background`, `correction`, `first_action`, `shared_state`, `peer_rules`,
  `autonomy`, `idle` (a session with a queue, a backlog or executors: not an executor, not the liaison), `helpers` (the same sessions: work handed off goes to the hub's executors), `findings`, `contacts` (not the liaison), `gist`, `cards`, `progress`, `lessons` (an agent, not its executors), `executor`, `minihub`, `minihub_dirty`, `bus`, `roster`, `rules`, `critic`, `backbone`, `skills`,
  whichever the session has. Part names and texts are not a promise of an API version: they change with core releases (a
  name change is listed in CHANGES). On `--resume` the module is called only if Claude Code understands
  `--system-prompt-snapshot` (otherwise it sends the prompt of the conversation's first request and the change would not
  arrive). A running session receives the new addendum after `hub session reload`.
- **`POST /hook/prompt.input`** is called on every message the host writes to a session. Request: `{session, text, message}` (the text with the core's `[hub:mN …]` labels; `message` is the message number or `null`). Response: `{text}`.
  The core tracks delivery by message number, not by labels: a module may remove the label too, and the session then simply
  will not see the number.

`hub show <session>` writes what the modules changed under the first line: "module X: addendum, added …; replaced …; removed
…; reordered …" (on the latest process start), "module Y: changes the text of session messages" (the `mod.prompt` fact,
written by the core). The project's and the hub's CLAUDE.md are read by Claude Code itself from the session folder, so
there is no point for them. The tmux host does not call these points.

### The `core.write` right: writing core events and changing how they are handled (Module API 2.2)

A module with this right can break the hub and all sessions: the core is not responsible for the state after it, and
disabling the module does not roll back its edits. So it loads only if you also named it in `modules.core_write`:

```json
"modules": {"paths": ["~/hub-modules"], "enabled": ["notes"], "core_write": ["notes"]}
```

**Core events.** A fact in the spool with the top-level field `"module": "<name>"` from a ready module with the right can be
of any type except `core.*`, `module.*` and another module's `mod.<other>.*`. The core writes it with the fields `"by": "<name>"` and `"src": "mod:<name>"` and folds it with the ordinary code; the core does not touch such a fact's `channel`,
`owner` and `sender`, so with the right the module speaks for anyone, just as it folds for the core. Without the right it is
as for all modules: its own `mod.<name>.*`, and anything else is rejected.

**Intercepting handling.** In `module.json`:

```json
{"capabilities": ["core.write"], "fold": ["message.received", "timer.*"], "fold_state": ["session", "queue"],
 "actions": true}
```

- `fold` lists event types or `x.*` families (not `core.*`, `module.*`, `mod.*`). One module intercepts one type: two modules
  with overlapping `fold` are both `blocked` until `modules.use.fold` names one.
- For such an event the core calls `POST /hook/fold` `{api, event, part}` **before recording**: `event` comes with its `seq`
  number and time, and `part` holds the parts of state from `fold_state`: `session` (the event's session; for
  `message.received`, the addressee), `queue` (its messages in the queue and being typed), `owner`, `account`, `budget`,
  `limits`, `runs`, `mods`.
- The answer: `{"mode": "core"}` is the core's handling (the default); `{"mode": "after", "patch": [...]}` is the core's
  handling and then the edits; `{"mode": "replace", "patch": [...]}` is the edits only; `{"mode": "reject", "reason": "…"}`
  rejects the event like a core rejection (whoever waited for an answer sees the reason).
- An edit is `{"op": "set", "path": ["messages", "m4810", "urgent"], "value": true}` or `{"op": "del", "path": [...]}`, up
  to 256 of them; a path is dict keys, and a list can only be replaced whole. `seq`, `core_written`, `modules`, `mods`,
  `mod_dedup` and keys starting with `_` are not touched; a new `m<number>` with a number greater than the event's `seq` is
  rejected (it is the number of the core's future message).
- The common tail of the fold (the queue, input typing, bring-up) always runs, on the edited state. The feed, the steps and
  the "Flow" are built from the event itself: `replace` changes only the core's state; to cancel an event, use `reject`.
- With `"actions": true` there is also `POST /hook/actions {event, actions}` with the ready list of actions of this entry.
  The response is an edit by number: `{"drop": [1], "replace": {"0": {"text": "…"}}, "add": [{"do": "interrupt", "session": "a"}]}`. You can replace `text` and `attachments` on `type`, `text` on `compact`, and `response` on `respond`; you can add
  `type`, `respond`, `release`, `kill`, `interrupt`, `end_tree`, `recap`, `compact` (not `spawn`: bring up or restart by
  editing `sessions.<name>.dirty = true` or by your own event). A removed `type` does not cancel the delivery wait: if you
  take delivery over, remove the message from the queue with an edit in `fold`.
- The module's decision is written into the same journal line, in the `fold` field; the page, the bot, the history and
  `hub` fold what is recorded with the core's code, without the module.

**Failures and budget.** No answer, garbage, an edit that did not apply, state outside the core's state machines, or a
fold with edits that was rejected: the core handles the event, `module.error` follows, and a pause follows as for all points.
All of a module's calls in one pass of the core tick take at most 1 s, and at most 6 s per minute; beyond that the event
goes without the module, with the line `"fold": {"mod": …, "skipped": "budget"}` (likewise `suspended`, `not_ready`, `part`,
`stopping`). Interception is not guaranteed: the module sees the skips in the journal.

**Always visible.** Before a module's first write with `by`, or an edit, the core puts a `core-written` file in the state
folder and a mark in the state; `hub modules` and "About the hub" say "a hub with a module that writes into the core's
state"; the module's rows in the feed carry an icon, in the "Flow" a dotted line, and in `hub show` `[mod:<name>]`.
`hub modules touched <name> [--days N]` shows what the module changed and where it is now; `hub modules forget <name>` (you
or the hub) clears the warning from the page. A core rollback to a version that does not understand such records will not
come up; a rollback to a version before Module API is not protected.

## Page plugins: API 1.36

The `web` field is an ES module in the plugin folder. The page loads it and calls the default function:

```js
export default function register(hub) {
  hub.addView({ id: "hello", title: "Hello", subtitle: "my plugin",       // its own view: an item on the left, #/x/hello
    render(root, snap) { root.replaceChildren(hub.el("p", null, `Sessions: ${snap.sessions.length}`)); } });
  hub.addSettings({ id: "hello", title: "Hello", render(root, snap) { … } });   // its own tab in "Settings"
  hub.addSlot("topbar", { id: "busy", render(root, snap) { root.textContent = "…"; } });   // an element at a place on the page
}
```

`render` is called on every core snapshot with the same `root`, so the element's state is not lost.

**View controls in the page toolbar (1.31).** When `hub.capabilities.viewToolbar === 1`, a view may provide
`toolbar(root, snap)`. The host mounts this root in the shared topbar only while the view is open. The root
survives snapshots, leaving/returning, and re-registering the same view's metadata from the same plugin.
Create controls once and update them in place to retain search text, focus and selection. An empty root
uses no space; on a narrow screen the controls wrap and fields shrink, so do not give them a fixed width. Older hosts
ignore this callback; keep a fallback inside the view when the capability is absent.

**A number on the ☰ item (1.36).** `badge` in `addView({...})` is a positive whole number shown as plain text at the right
of the view's ☰ item (0, a string, a negative or fractional number or anything else shows nothing). To change it, register
the view again with the new number. Older hosts ignore it; `hub.capabilities.viewBadge === 1` tells whether this one shows it.

Since 1.1 a view also has `show(root)` and `hide(root)`, called when it is opened and left: start and stop animations and
server polling there; `icon` is the item's icon: a name from the hub's icons (`"flow"`, `"clock"`, `"board"`…) or a
plugin file (`"icon.svg"`, painted in the text color); `full: true` makes the view fill the whole area with no padding (a
canvas). `hub.icon(name)` is a hub icon, `hub.url(file)` is the address of a plugin file, and `hub.addStyle(file)` attaches
its CSS. A live example is the preinstalled "Flow": `modules/web/bundled/flow/`.

**Navigating to a view item (1.28).** `hub.go(id, params)` opens a view and straight away the right item in it: `params` is
a plain object with string, number or `true`/`false` values (`null` and `undefined` are skipped), for example
`hub.go("inbox", { plugin: "teams", id: "p7" })`. They go into the address, `#/x/inbox?plugin=teams&id=p7`, so a page
reload, a link and the browser's "back" preserve them. The view receives them in `open(root, params, ctx)` as strings
(`{ plugin: "teams", id: "p7" }`):

- when entering the view (and `{}` if it was entered without `params`, from the item on the left or via `hub.go(id)`);
- when moving to the same view with different `params`;
- on a repeated `hub.go` with the same `params`: the view is already open, and the owner asks for this item again;
- on registration, if the view was navigated to before the plugin added it (`params` wait in the address).

`open` is called after `render` and `show`, when `root` is already in the page (so you can scroll to the item). Two
navigations in a row: the view receives only the last one. `ctx.from` is the address the view was entered from
(navigations inside the view do not change it; if the view was opened by a link, it is `null`), and `ctx.close()` returns
there (to the session list if there is nowhere to go back to); on returning to the plugin's view with its `params`, the
plugin receives them in its `open`. `hub.go(id)` without `params` works as before. A value that is not a plain object means
no navigation, and the error appears in Settings → Plugins.

```js
hub.addView({ id: "inbox", title: "Inbox", render(root, snap) { … },
  open(root, { plugin, id }, ctx) { if (id) select(plugin, id); back.onclick = () => ctx.close(); } });
```

**The view registry (1.28).** `hub.views()` returns a new array of `{ id, title, icon, order, owner }`: the built-in menu
items `settings` and `schedule`, then the views added with `hub.addView`, in registration order. `title` is the heading in
the page language (a plugin's own for a plugin), and `icon` is a hub icon name or the address of a plugin file (also of a
core module's plugin). `owner` is the name of the plugin from `extensions` (`"mod:<name>"` for a module), and `null` for
built-in ones; a plugin can give a view the id of a built-in item, so tell them apart by `owner`. `order` is the position in
the menu from zero. This is only a description: there are no nodes or functions in it, and editing the array does not
change the hub. Items drawn by the `sidebar.nav` place are not views and are not in the registry.

`const off = hub.on("views", rows => { … })` subscribes to registry changes and returns an unsubscribe function. The
initial list is `hub.views()`; the subscription does not send it. Several registrations in one turn produce one
notification (as a microtask); a new view and a replacement of the same id's description notify, a repeat with the same
description does not. Each handler gets its own copy; a handler error goes to Settings → Plugins, and the others keep
working. `hub.on` has no other events yet.

```js
const redraw = () => drawTiles(hub.views());
const off = hub.on("views", redraw);
redraw();
// a plugin view is opened with hub.go(row.id); built-in ones are #/settings and #/schedule (owner === null)
```

**Opening a settings tab (1.28).** `hub.openSettings(tab = "about")` opens Settings on a tab: built-in ones are `"about"`,
`"general"`, `"modules"`, `"accounts"`, `"plugins"`, `"theme"`; a plugin's tab (`hub.addSettings({ id: "sidebar", … })`) is
`"x:sidebar"`. `true` means the tab exists and is selected (as if clicked, and the choice is remembered; if Settings is
already open, it switches at once), and `false` means there is no such tab and no navigation: a plugin registers its tab
earlier. The preinstalled "Usage" is a ☰ view (`#/x/usage`), no longer a tab: `openSettings("x:usage")` opens that view.

To check whether the hub has this, use `hub.capabilities?.views` and `hub.capabilities?.openSettings` (a hub older than
1.28 does not have them).

**Sidebar controls (capability `sidebar: 1`, 1.31).** `hub.sidebar()` returns
`{open, available}` for the core left sidebar. `hub.sidebar({open: false})` closes it;
`open: true` restores it. The core remembers the choice and retains the current route,
view, draft, and resized width. Only a boolean changes state. On phone layouts
`available` is false and the call does not change the desktop preference; use the
normal sections route there. `hub.on("sidebar", fn)` observes open/closed changes
from either plugins or the divider and returns an unsubscribe function. Each listener
receives a detached `{open}` object. Check `hub.capabilities?.sidebar === 1` on older hosts.

A theme may set `--sidebar-default-width` on `.app`, including in media queries.
The unchanged core fallback is 272px. An explicitly resized `hub.leftW` overrides
this default; opening/closing the sidebar does not convert the default into a fixed
width. Theme defaults should stay within the supported divider range (200–520px).

**The page language (1.25).** `hub.lang` is the page language code (`ru`, `en`, `de`…; `xx` is a test pseudo-language),
`hub.locale` is the language for dates and numbers (`toLocaleString(hub.locale, …)`), and `hub.t(key, {substitutions, count})` is a string from the core catalog (`i18n/<language>.json`, `web.*` keys). A plugin keeps its own strings and picks
them by `hub.lang`. The hub chooses the language: `language` in hub.json, then the owner's language, then (for a hub whose
history began before languages existed) Russian, then the browser language, then English (`i18n/README.md`). The same page
strings are available as `GET /i18n.json` `{lang, dir, messages, fallback}`: the language's strings, and the English ones
only for what the language lacks.

**The plugin channel (1.2)** is posts to the hub journal: a session writes `hub post <plugin> "<text>" [--reply pN]` in its
own name and reads with `hub feed <plugin> [--last N]`; the plugin process (`run`) writes in the plugin's name
(`HUB_EXTENSION`), not the owner's; the page uses `hub.feed(since)` and `hub.postFeed(text, reply_to)` (the owner's post),
and the snapshot's `snap.feeds[<plugin>]` holds the number of the latest post. The author cannot be forged, the channel
keeps the latest 500 posts, and a post is up to 20,000 characters (2,000 before API 1.22; longer is rejected, not
truncated). An older post (past the 500) is read back from the journal by its number: `GET /api/feed?plugin=<name>&post=p<seq>[&post=…]` (API 1.32, up to 20; `features` has `feed.post_by_id`), page plugin `hub.feedPosts(['p123'])`; an unknown number or another channel's post is skipped, a bad number gives 400. An example is the "Agent feed" plugin (`agent-twitter`).

**Channel posts in the event stream** (API 1.36, `features` has `stream.feed`): `GET /api/stream?feed=<plugin>[,<plugin>…][&feed_since=<N>]` —
besides the usual `snapshot` and `patch`, the stream sends `event: feed` with `{"plugin": "inbox", "posts": [{"id": "p812", …}]}`:
the rows `/api/feed` gives, in order, for each new post of the named channels (up to 8 names). A post never changes, so an event is
always a new post. They start after what the stream's own first snapshot holds — its `feeds`, the last number of each channel (the
history is `/api/feed`), so nothing falls between that snapshot and the events; a stream opened while the page is still
reading the journal (right after its restart) starts with an empty snapshot, and the start is then the first patch that
brings `feeds` — read the history up to its numbers. With `feed_since` they start after post number N
instead: a post's number is its journal number, one count for all channels, so a client that comes back gives the `seq` of the last
snapshot it held — a post comes before the patch that brings its number, so none is skipped (a browser `EventSource` reconnects to the same address by itself: drop a post whose `id` you already have). Away
longer than the channel's window (its last 500 posts): you get the window. A stream without the parameter gets exactly what it got before.

**Preinstalled plugins** live in the hub repo, `modules/web/bundled/<name>/` (the same `hub-plugin.json`): they are enabled
at once and update together with the hub; in "Plugins" they can be turned off (this is all that is stored in settings) but
not deleted. With `"default_off": true` in the manifest a plugin is off until it is turned on in "Plugins".

**Sessions in the snapshot (1.4)** have roles: `agent` (a specialist), `executor` (an executor for a task), `worker` (the
rest), and the hub is `kind: "hub"`. An executor has `raised_by` (who brought it up) and `backlog` (leads a direction). A
question does not hold the turn with a form: it sits in the session's `pending` as before, but the session is not in
`dialog` (`dialog` is only for tool permission requests). A session question that the hub showed in its own chat (`hub question show`) is in the hub's `shown` (with a `session` field, who asked; the answer goes to it); it is in `pending` only
for the one who asked, so count questions by `pending`. All of this is additive: 1.x plugins work as before.

Places (`addSlot`):

| Place | Where | `ctx` |
|---|---|---|
| `sidebar.header` | the sidebar header: in the search row, before ☰ (1.5) | none |
| `sidebar.tiles` | the top of the sidebar, above the search row (1.30): earlier than search in the markup, so Tab follows the visible order in all browsers (section tiles); empty means no padding | none |
| `sidebar.top` | below the search in the sidebar | none |
| `sidebar.nav` | in the sections menu (the ☰ button in the search row), next to "Settings", "Schedule" and "Flow"; visible when the menu is open, and drawn on every snapshot even when it is closed (sizes are then zero) | none |
| `sidebar.footer` | the sidebar footer | none |
| `sidebar.session` | at a session row in the list | `{session}` |
| `topbar` | the top strip of the page | none |
| `session.header.start` | the header of the open session, before the name, after the back-to-the-list button where it is shown (a narrow screen) (1.35): earlier than the title in the markup, so Tab follows the visible order (a badge or a mark before the name, a plugin's own buttons); empty means the header is as before | `{session}` |
| `session.header` | the header of the open session | `{session}` |
| `session.composer` | above the open session's input field | `{session}` |
| `session.aside` | a panel to the right of the open session (1.6) | `{session, close, open}` |

**The right-hand panel (1.6)** is `session.aside`: if the place is left empty, there is no panel; on a narrow screen it
opens with a button in the session header, over the feed. An element's `title` is the caption of this button (a string or
`title(snap, ctx)`, for example "Notes · 3"), `ctx.close()` closes the panel, and `ctx.open()` (1.14) opens it yourself: the
plugin showed its content in response to an event (a file on click), even if the owner had closed the panel before. Call
them from your own event handler, not from `render`: if the panel is already open (closed), the call does nothing.

A file in the panel is a "Files" window in an iframe: `<Files address>/open?path=<path>&embed=1` (the Files port is
`files_port` in the snapshot): only the file, without the folder list; navigating to a folder (the path above the file, the
cross, Esc) leaves this view for the full Files window. HTML reports are not shown in the embedded window: their `/render`
page allows embedding only into Files itself (CSP `frame-ancestors 'self'`); open such a file without `embed`.

**The window without its own header (1.36).** Add `&chrome=hub` to `embed=1` (`/open?path=<path>&embed=1&chrome=hub`, with an
optional `&theme=<theme id as /api/themes gives it>`) and the Files window hides its own header, so the plugin draws one
header for the panel. The window then talks to the page by `postMessage`, and only with the hub page's exact origin (the
page's host and its port, `html[data-hub-port]` of Files): it never posts with `"*"`, and it ignores a message that is not
from its parent window at that origin (a report page inside the window cannot command it). Without `chrome=hub` (or
without `embed=1`) nothing is posted and the window is as it was.

- Window → page, `files:state`: `{type: "files:state", title, crumbs: [{id, label}], actions: [{id, label, on?, off?, href?}], editing,
  info: {path, size, changed} | null}`, after every change of the file, the view or the mode. `crumbs` are the folders above
  the file (the first is the Files home), `title` is the file or folder itself; an action with `href` (the download, "Open
  in VS Code" on a Mac) is a link the page opens itself on the owner's click.
- Window → page, `files:close`: `{type: "files:close"}`: Esc was pressed in the window, the page closes its panel.
- Page → window, `files:do` (`frame.contentWindow.postMessage(msg, <Files origin>)`): `{type: "files:do", id}`. Only an `id` from the
  last `actions` or `crumbs` the window told is run; nothing else in the message is read, a path or an address in it is not
  taken. These ids are navigation and reading only: a crumb (`crumb:home`, `crumb:0`, …), a view (`view:page`, `view:src`),
  the neighbouring files (`prev`, `next`), and `edit`, which only switches the window into its edit mode: saving and
  cancelling are the buttons under the field in the window itself, and nothing that writes a file or starts a program can be
  asked for by id.
- The page side checks the same way: it takes `files:state` and `files:close` only when `e.source` is its Files frame and
  `e.origin` is the Files origin, and opens an `href` from `actions` only when it is `http(s):` or `vscode:`.

**Holding a restart (1.27).** A plugin that holds something not yet on the server (a draft being written, undelivered
messages) sets the `data-hub-hold` attribute on `<html>`: while it is set, a hub restart waits (the whole thing: the page
(`web`) and the plugin processes (`ext:*`), including the automatic one after an update), for no longer than two hours
from the request (or `restart_wait_minutes`, if that is longer; 0 — no ceiling), and the tab does not reload itself to
the new version; a closed tab releases it at once, and a crashed one does so by itself after two and a half minutes. The
attribute value is a name for `hub status` (`data-hub-hold="my-draft"`); an empty one means "a plugin draft".

**An input field like the chat's (1.7)**: `hub.input({ to, key?, placeholder?, hint?, prefix?, wake?, onSent? })` returns `{ node, focus(), set({...}) }`: the same field as in a session conversation. It grows with the text, Enter sends, Shift+Enter
adds a new line, the draft is not lost, and it has a paperclip, image paste with ⌘V and file drag-and-drop (an `[Image #N]`
label in the text), voice by button and by Space in an empty field, and an error returns the text and files. The message
goes to session `to` from the owner; `prefix(text)` is what to prepend to the text (for example, what it is about), and
`wake()` says whether to wake a sleeping executor. Keep the node on your side between snapshots (like `root`): then the
cursor and the typed text stay. `set` changes the addressee, the hint or the prefix on the fly.

**Input transport (1.28)** is for a plugin that delivers messages from its own field itself (a queue, a retry, its own
"sending" line) while taking the field and draft from the page. To check whether the hub has this, use
`hub.capabilities.composerTransport === 1` (`hub.capabilities` is what the page can do: name to version; no name means no
capability).

- `hub.send(body)` is the standard delivery of the owner's message: `POST /api/send` with a `client_id` (your own in `body`
  or a new one) and a receipt `{ ok, receipt, client_id, status, data?, error? }`. `receipt`: `accepted` means the core
  took it (since 1.29, the core's decision: `data.seq` is the event number, `data.id` is the message's `mN` or `null`; a
  repeat of an already accepted `client_id` also gives this, with `data.already: true` and the number of the first: there is
  one message); `refused` means the hub or the core refused, with the reason in `error` (`status` 4xx; a core refusal is
  409, with `data.code` as the reason code if there is one), and there is no point repeating it; `queued` (1.29) means the
  core did not decide within 3 s (busy or not running), with `ok: true`: the outcome comes later via `hub.receipt`;
  `unknown` means there is no response, a drop or a server failure: it may or may not have arrived, and a repeat with the
  same `client_id` within a day is safe. `status` is the response code, and `0` means there was no response.
- `hub.receipt(session, clientId)` (1.29; to check for it use `hub.capabilities.receipt === 1`) gives the outcome of a send
  later: `GET /api/send/receipt` → `{ receipt, … }`: `accepted` (`seq`, `id`) means the core took it; `pending` means it is
  still in the hub's spool and the core has not taken it; `refused` (`error`, `code?`, `seq`) is a refusal that the page
  remembers (the latest ones, while they are in the current journal); `unknown` means the core never took it and it is not
  in the spool: sending the same words again makes one message; `null` means the server did not answer. After `queued`
  the page's input field asks this way itself: a refusal puts the words back in the field with the reason, and 10 minutes in
  the spool gives the caption "still in the spool". A field with its own transport (`send`) does not ask for the receipt on
  the plugin's behalf: `queued` from it means "sent".
- `hub.input(…)` fields (and the Hubs composer) show a second, quiet microphone next to Send while the field has text or attachments: it dictates into the field at the caret and sends nothing; with an empty field only the voice-message microphone is there. It stays visible while a dictation records or recognizes, so it can always be stopped.
- `hub.input({ …, send(body) })`: the field sends through your transport. `body` is exactly what would go to `/api/send`
  (`session`, `text`, `attachments`, `wake?`, `client_id`). Return the `hub.send` receipt or your own `{ error, client_id? }`:
  with `error` the text and files return to the field with your phrase; without `error` it counts as sent. Without `send`
  the field delivers by itself, as before, and also with a `client_id`.
- Sending with an unknown outcome (`receipt: "unknown"`, or `error` with `client_id`): the owner sends the same thing once
  more (the same addressee, text and files), and the field takes the earlier `client_id`, so the core does not duplicate the
  message if the first one arrived.
- The draft: `input.draft()` returns `{ text, files, uploading }` (`files` is the attachment metadata from `/api/upload`,
  `uploading` is how many are still uploading); `input.set({ text?, files? })` replaces the draft: restores a saved one
  (after a tab reload) or clears the field when your retry delivered the message. The plugin itself stores the draft across
  reloads.

**Dictation into a field (1.31)**: `hub.dictate(field, { onText?, onError?, space? })` returns a microphone button for your
field (`input` of text types, `textarea` or `contenteditable`): place it next to the field and keep it between snapshots.
To check for it, use `hub.capabilities.dictate === 1`. The recording is the same as for a voice message: hold, speak,
release; a click starts recording and another click stops it; Space in an empty field (only while the caret is in that
field; outside fields Space is the session's voice message; `space: false` means Space does not start recording, only the
button does); Esc cancels; the microphone permission is the same. The transcription goes into the field at the caret
(replacing the selection; if the field was not focused, at the end), spaced from the neighbouring words, and the field
gets an `input` event: nothing is sent. If voice is not set up on the hub or the browser gives no microphone, the button
is hidden (`hidden`) and appears by itself once voice is on; it is hidden for a read-only or disabled field too (a
recording already running into it goes to the end). `onText(text, { inserted })` fires on a transcription:
`inserted: true` means the text is in the field, `false` means the field was rebuilt while recognition ran or became
read-only or disabled, and the text is yours (without `onText` it goes to `onError`). `onError(text, { retry?, download? })` is what to tell the owner (on the
button it is also the tooltip): the microphone was refused, nothing was recognised; `retry()` and `download()` are there
when the recording was not transcribed and is kept for a retry; `retry()` does nothing while another recording runs. An
exception in a callback does not break the recording: it goes to Plugins, like other callbacks. Not a field: `null` and
an entry in Plugins.

**Hub words in the banner (1.30)**: `hub.banner(text)` shows a line in the page banner, where the page writes its own errors:
it fades after 10 s, and a click closes it. This lets a plugin or a module's view show the hub's refusal of its request (a
4xx response with `error`) as the page does, without touching its markup. The text is plain text only (HTML is not parsed),
one line, at most 300 characters. While the page holds its own banner ("no connection", "the page was updated"), it is not
overwritten, and it is not shown where the banner is not visible (a narrow screen, the session list): then the result is
`false`, and `true` if it was shown. To check for it, use `hub.capabilities.banner === 1`.

```js
const r = await hub.post("/api/mod/my-module/toggle", { on: true });
if (r?.error && hub.capabilities.banner) hub.banner(r.error);
```

**The message of a feed row (1.36)**: `hub.messageAt(node)` tells a plugin which message a feed row shows, so it can offer
its own actions on it (a menu on the right click or a long press, a shortcut). `node` is the row or anything inside it; the
result is a frozen `{ session, kind, mine, target, text }` or `null` when the node is not in a feed row or the row is a
service line (a restart, "in the schedule…"), not a message:

- `session` is the session whose feed is open; `kind` is the feed row kind (`in` a message to the session, `out` its reply,
  `file`, `notify`…); `mine` is `true` for the owner's own words;
- `target` is what `POST /api/react` takes for this row (`{session, target, emoji, quote}`, needs the "Reactions" module),
  the same rows the feed's own reactions use: a session reply and the owner's own message; `null` means the row takes no
  reaction (also while the module is off);
- `text` is the message as it was written (markdown), `null` for a row without one.

The core adds no menu and no handler of its own: the plugin listens to the events itself and decides what to do. A
reaction does not wake the session; a plugin that wants it to act at once sends words as well
(`hub.send({ session, text, wake: true })`). To check for it, use `hub.capabilities.messageAt === 1`.

```js
document.addEventListener("contextmenu", (e) => {
  const m = hub.capabilities.messageAt && hub.messageAt(e.target);
  if (!m?.target || e.target.closest("a, img, input, textarea")) return;   // the browser's own menu stays
  e.preventDefault();
  openMyMenu(e.clientX, e.clientY, (emoji) => hub.post("/api/react", { session: m.session, target: m.target, emoji, quote: m.text }));
});
```

**Order in the session list (1.10)**: `hub.addOrder((group, rows, snap) => rows)` lets a plugin reorder a group's rows
(`"agents"`, `"directions"`, `"workers"`; since 1.16 also the owner's own groups, with ids like `g-…`), for example pinned
ones on top; it cannot lose, duplicate or add a row, otherwise its order is not used. The owner's own order (drag and drop,
`layout.order`) is applied on top of the plugin's order; a group the plugin does not know, it returns as is. This is the
group order for the "All" view: in "Now" (since 1.24) rows go in sections by state ("Waiting for you", "New", "Working",
"Waiting", "Just viewed"), each sorted by time, and `addOrder` does not reorder them. `hub.redraw()` redraws the page when
the plugin's own state changed (not the snapshot). An example is "Pinned directions" from the catalog
(`hub-plugin-pin-directions`).

**Subgroups in a session list group (1.31)**: `hub.addSubgroups((group, rows, snap) => [{ id, title, rows, open? }] | null)`:
the plugin splits a group of the "All" view into subgroups with headers (for example, "Working / Waiting for others / Done"
inside "Directions"); `null` leaves the group whole. Every row of the group goes into exactly one subgroup, otherwise the
split is not taken (as with the order); the row order inside is as in the group (`addOrder`, the owner's manual order),
empty subgroups are not shown, and the first plugin that split the group wins. The core draws the headers and folds them
with the same mechanism as its own groups: the arrow, the count, a dot on a folded one if someone under it waits for you;
the browser remembers the owner's choice; `open: false` keeps a subgroup folded until the owner opens it; search opens
everything. `hub.groupFold(id, open?)` tells whether a group is open (`true`/`false`); with `open` it folds or opens it, as
its header does. `id` is a group (`"directions"`, `"g-…"`), a "Now" section (`"a:wait"`) or a subgroup
(`"<group>/<subgroup>"`, for example `"directions/done"`). Whether the page has it: `hub.capabilities.subgroups === 1`.

**What counts as unread (1.25)**: `hub.addUnread((session, snap) => true|false)`: the plugin decides what is "new" at a
session row and in "To review"; if it returns something other than true/false, the core's rule stays. For this, a snapshot
row has `owner_reply`, the number of the latest outcome addressed strictly to the owner (a turn on the owner's message or its
continuation, an answer to the owner's task; core and session service messages do not count), and `seen`, how far the owner has looked.
Since 1.36 the row also has `reply_head` and `reply_ts`, the start (up to 160 characters) and the time of that answer: a
later result the session sends on its own replaces `out_head`, these stay (null before the first answer or on an older core).
An example is `hub-plugin-quiet-unread` from the catalog.

**Checkboxes on the Look tab (under the feed) (1.12; before 2026-10-16 on "Settings")**: `hub.addOptions({ title, hint?, options: [{ key, label, hint?, on }] })`: a group of the plugin's toggles next to the feed settings; `on` is the default
value. Read with `hub.option(key)`; the choice is stored in the browser, and on a change the page redraws (the render of
places and views is called again). A whole tab of your own is still `hub.addSettings`.

**A theme's own settings (API 1.37)**: `hub.addOptions({ themes: ["dusk.json"], hint?, options: [{ key, label, hint?, on }] })`
from the code of a plugin that ships the theme. The group stands in Settings → Look right under the row of that theme,
and only while it is chosen: with another theme it is nowhere on the page. A theme is named by its file from the
plugin's manifest (`themes` there); its id is `<plugin>:<file>`, as `GET /api/themes` gives it, so a plugin cannot put
settings under another plugin's theme. A call with `["a.json", "b.json"]` puts the group under both, replacing this
plugin's earlier group there. The values are the plugin's, as before: `hub.option(key)` gives the choice under any
theme (your code acts only while its theme is chosen), and with no choice saved the default comes from the chosen
theme's group. To check: `hub.capabilities.themeOptions === 1`. A core before 1.37 does not know `themes`: the group goes
to "Look" with its `title` (none — an error under Plugins), and a plugin keeps only its last group there, so on such a
core declare one group with a `title`, or none. The core's Clean declares its ticks this way.

**Commands via "/" (1.13, not yet released)**: `hub.addCommand({ name, args?, hint?, by?, run(args, ctx) })`: the owner types
`/` in a session's input field and a command window appears above the field: first the core's commands (`/compact`,
`/reload`, `/model`, `/effort`, `/stop`, `/away`, `/back`, `/restart`), then the plugins' commands. `/name arguments` plus
Enter makes the page call `run` itself, without the model and without the session; `ctx` is `{ name, session }` of the open
session. If `run` returns a string, it appears as the hint under the field; if it throws, its text appears under the field
and what was typed comes back. `args` is a hint in the window: `"<text>"` is required (picking the row appends the name and
waits for it), `"[hours]"` is optional. Core command names are taken; if two plugins use one name, the first works and the
second gets an entry in "Plugins". Only what is in the list is executed: any other "/name" (a skill, someone else's command)
goes to the session as ordinary text, as it does without the command window.

```js
hub.addCommand({ name: "new-task", args: "<text>", hint: "a new task",
  run: async (text) => { await hub.postFeed(JSON.stringify({ kind: "create", title: text })); return "Created"; } });
```

The order within a place is `order` (lower comes first). `hub.api` is the hub's plugin API version; `hub.snapshot()` is the
core snapshot (sessions, messages, open tasks as `delegations`: `working`, `waiting-owner`, `spec` and `stopped`, the schedule…);
`hub.post(path, body)` performs page actions, for example `hub.post("/api/send", {session, text})` (a button, rather than
the owner's own words, should use `action: true`: such a message does not become an answer to an open session question
card); `hub.go(id, params?)` opens a view (since 1.28 also an item in it); `hub.el(tag, cls, text)` creates an element.
Files are served only from the plugin folder (`/ext/<name>/…`: js, css, svg, json, png; since API 1.32 also its media, here and under a core module's `/mod/<name>/web/` — jpg,
webp, gif, mp4, webm, mp3, ogg, wav, wasm and glb, with their own types, video and sound by `Range`, so `<video src>` seeks
without loading the whole file; `fetch` a `.wasm` with `WebAssembly.instantiateStreaming`, a `.glb` with any glTF loader).
A path that leads outside the folder, a symlink out included, is a 404. A plugin error does not break the
page: its place stays empty, and the error is in Settings → Plugins. When a file changes, open pages reload by themselves.

**Stylesheets without the jump (API 1.37).** `hub.addStyle(file)` runs only after the page has loaded the plugin's code, so
a plugin whose CSS changes a token (`--column`, the width of the feed, a color) showed the standard look for a moment and then
jumped. List the files in the plugin's record instead: `"css": ["web/look.css"]` in `hub-plugin.json` (copied to the record in
`hub.json` on install and update; a hand-written record in `hub.json` takes the same field). The page puts each file into its
own `<head>` as `<link rel="stylesheet">` after the hub's stylesheets, so the first frame already has them. Up to 8 files, each a
`.css` inside the plugin folder (Latin letters, digits and `_ . / -`; no `..`, no leading `/`); a file that does not fit is
dropped, the rest stays (the install and the catalog check do not refuse the plugin for it; the catalog check warns). The page
waits for these files before it paints, as for its own: keep them small. Only for a plugin with a page module (`web`); a theme-only plugin does not take it, a theme
styles the page only while it is chosen. A page of a core before 1.37 ignores the field, so a plugin that must also work
there calls `hub.addStyle` as well: for a listed file it adds nothing. A plugin that appears while the page is open
(an install, an enable) has its files attached before its code runs. A core module's page plugin (`web` in `module.json`)
has no such field yet: it keeps `hub.addStyle`.

**Themes** are a plugin without code: `"themes": ["nord.json"]` in the manifest and the theme JSON,
`{"name", "dark", "colors": {"background", "foreground", "primary", …}}` (values of the `tokens.css` variables; ones not
set are derived from the background, text and accent). Plugin themes are in Settings → Look; your own files go in
`themes/` of the hub's settings folder (`~/.config/hub-core/themes/` for the main hub; `GET /api/themes` names it as `folder`,
since 1.36).

Theme art is a picture next to its JSON: `"art": {"image": "city.jpg", "veil": 0.8, "position": "center 30%", "glass": true, "credit": "Photo: author, source"}`. The picture is laid as the page background under a veil of the `background` color
(`veil` is the density, 0.5-0.95: the brighter the art, the denser, so that the feed text stays readable); `glass` makes
panels translucent with blur; `plates` puts a plate under each feed block (a busy art: the text stays readable); `credit` is
the caption in the theme list. The file is jpg, png or webp up to 8 MB inside the plugin folder (or the folder of your own
themes); a width of 2400 px is enough.

Since API 1.3 a theme changes the look of the hub itself too: `"icons": {"account": "i/account.png", "clock": "i/clock.png", …}` gives its own icons in place of the standard ones (names as for the hub icons: account, settings, clock, flow, send,
search, board…); `"avatars": {"🛠️": "a/wrench.png", …}` gives pictures in place of session emoji (an emoji without a picture
stays an emoji, and since API 1.31 it carries a `data-emoji` attribute with the emoji itself: theme or plugin CSS adjusts the
optical center of a specific one, `[data-emoji="👍"] { transform: translateY(-1px) }`; the value is exactly the character in
the session badge, with all invisible characters (`🛠️` comes with U+FE0F), so copy it from there); `"font": "f/pixel.woff2"` is the headings font (the feed and the names in the list stay readable). Since
API 1.8 there is also `"font_scope": "all"`: the theme font for the whole page, both text and code (the standard ones serve
as a fallback for missing characters; `"titles"` or no field means headings only). A wide font will shorten the names in the
list on the left, so for the whole page take a narrow monospace font. `"font_scale": 1.2` makes the theme font larger or
smaller (0.8-1.5), while the layout and the fallback font do not change: pixel fonts look smaller at the same px. Files sit
next to the theme: images png, jpg, webp, gif, svg (shown as an image, not inlined into the page), fonts woff2, woff, ttf,
otf; only those declared in the theme JSON are served. A theme JSON carries no settings: its plugin's code declares them
(`hub.addOptions({ themes })`, 1.37). Since API 1.9 there is `"font_scope": "mono"`: the theme font only
where the font is monospace and in the system theme (code, paths, feed), while text and headings keep the system font.
Since API 1.11 there is the color `"attention"`: the header of "session waits for you" cards (a question, a tool, a
permission); without it, a muted `destructive` on the `card` backdrop. A theme with its own CSS that recolors the `.card`
card sets `--attention` (the header) and `--attention-line` (the border and the line between questions) there too; the
header corners follow the card corners.
Since API 1.31 there is the color `"code-bg"`: the background of code blocks in the feed and in the rendered text of "Files" (`--code-bg`; without it the
`--muted` backdrop, as before; a source view and the permission cards keep their own) and a plate behind inline code (a 2px ring around the text, so the line box does not change; code
inside a link has no plate). A theme with its own CSS that restyles code sets its own `background` or `box-shadow` on `.md :not(pre, a) > code`.
Since API 1.32 there are feed tokens that a theme (with its own CSS on `:root`) can override: `--radius-pill` (default `999px`)
is the rounding of the "New · N" mark, the counter and the "To the latest" button; `--jump-shadow` is the shadow of that button
(`none` for no shadow).
Since API 1.36 there is `"measure": "110ch"`: the longest line of the feed text and of the rendered text in "Files"
(`--measure`, 68 characters in the system theme). A wide monospace font makes 68 characters a narrow column with empty
sides; a theme with such a font says how many it wants. A number with `ch` from 40 to 200 (`"110ch"`, `"72.5ch"`), anything
else is ignored, and a theme without the field keeps the system width. A theme's own CSS may also set `--measure` on
`:root`, but only the field reaches "Files": it shows a theme's colors and fonts, not its CSS.
Since API 1.36 the core has its own themes. **Clean** is the look of a browser that chose no theme: light or dark as in
the system (`core:clean`) or fixed (`core:clean-light`, `core:clean-dark`); its colors and font are in
`modules/web/static/clean/palette.css`, its layout in `clean.css`, and all of it acts only under the `th-clean` class on
`<html>` (`th-clean-dark` for the dark one). **Classic** (`core:classic`) is the look of the core before Clean, with no
class. A plugin theme lies over Classic, as before: while it is chosen there is no `th-clean`, so a theme's own CSS and
a plugin's slots see the same page they saw. A plugin that styles its own elements for Clean writes the rules under
`html.th-clean`. `hub.theme` (and `files.theme` in "Files") holds `{id}` for the core's themes, `{id, theme}` for the
others.

Examples: `examples/plugin-slots/` (an element at each place and a tab in "Settings"), `examples/plugin-theme-nord/` (a
theme), `examples/extension-hello/` (a process and a view).

### Versions

The hub has a **plugin API version** (`core/version.py`, currently 1.37): the contract with plugins covers the manifest,
`hub.*` on the page, and facts into the spool. The minor number grows when the contract is extended (old plugins keep
working); the major number grows when something in it is removed or changed. A plugin states in its manifest which version
it works with: `"hub": "^1.0"`. A plugin for another
major version is not installed or loaded; a plugin that needs a newer minor version is installed with the warning "update the
hub"; one without the field is installed with a warning. Things deprecated in the API are flagged in advance: "will stop
working in 2.0". All of this is visible in `hub marketplace search` and in Settings → Plugins.

### Design tokens

The page's styling is CSS variables in `modules/web/static/css/tokens.css`; plugins and themes read and override them. All
names and values of the earlier version are kept: the `--n-0`…`--n-950` scale, `--space-1…4/6/8` (24 and 32px),
`--radius-sm/lg`, `--text-xs/sm/base/lg` (12/14/15/18px), colors, `--font-ui/--font-code`: a plugin written earlier looks as
it did before. What is new has new names: the neutral scale `--zinc-0…9`, the spacings `--space-5/7/9` (24/32/48px), the radii
`--radius-xs/xl/pill`, `--text-md/xl/display`, `--st-*` (state color), `--who-*` (feed participants), `--dur-*` and
`--ease-*` (motion), `--z-*` (layers). The `--text-*` font sizes are multiplied by `--ui-scale`: 1.0667 by default (the scale
one step larger), 1.2 and 1.3333 in Settings → Look, and at 1 the values equal the earlier ones. This does not affect a
plugin with `px` in its styles: only what uses `--text-*` is scaled. A theme with `font_scope` still overrides `--font-ui`
and `--font-code`.

### Optional control in the shared composer (1.31)

With `hub.capabilities.composerLeading === 1`, `hub.input({leading: () => node, leadingSig: key, ...})`
places a plugin-owned control before the action in the shared composer's bottom row. The node is retained across
draft changes and snapshots: `leading()` is called again only when `leadingSig` changes, so a new `leading`
without a new `leadingSig` keeps the old node — change both through `input.set()`; pass `leading: null` to remove it.
Recording temporarily replaces this row with the recording controls and puts the same node back afterward (it is
detached meanwhile: an open menu or focus inside it is lost). A button inside it needs `type="button"`, otherwise it
submits the field. If `leading()` throws or returns something other than an element (a text node or a fragment do not count), the field is shown without it
and the error is listed in Settings → Plugins. A field with a leading node does not stack under `--composer-wrap`
(below): the plugin's control keeps the one-row layout. Use `input.set({to})` to change the recipient independently
of navigation. The textarea, attachments, keyboard behavior, draft and send transport remain owned by the shared
composer. Older cores ignore these options; gate a UI that requires them on the capability.

### Start a voice recording from code (1.32)

With `hub.capabilities.inputRecord === 1`, `input.record()` on a `hub.input(...)` field starts a voice recording in that
field, as a click on its microphone does: the recording strip replaces the field, another click on the stop button (or
Space, or `Esc` to cancel) ends it, and the recognized words go to the field's addressee like a typed message.

- Call it from the plugin's own click or key handler (a "Comment" button, say). The microphone is opened synchronously
  inside `record()`, and iOS Safari hands it out only to a user gesture; from a timer or after an `await` it fails there.
  Dispatching a `click` on the microphone button does not work: the button listens to `pointerdown`.
- Returns `true` when the microphone started opening (a denial or a slow microphone is shown in the field afterwards,
  as for the button), `false` when nothing was started: voice is not configured on the hub, the browser cannot record
  (no secure page, no `MediaRecorder`), the field is not on the page, the field holds a draft (typed text or an attachment: the
  recognized words would replace it, as the microphone button is not there either), or a recording, a recognition or a
  dictation is already going. It says
  nothing to the owner in that case: decide in your UI what to do (keep the focus in the field, for example).
- Older cores have no such method (and no capability): check the capability before calling it.

### Optional shared composer wrapping (1.31)

A stylesheet can opt the shared `.composer` into full-width multiline text with a bottom control row using
`--composer-wrap: 1`. The core adds `.composer-stacked` after text wraps beside the actual attachment/action controls
or contains a newline, and retains it until the draft is empty. The stylesheet supplies both layouts and their dimensions;
the core retains the same textarea, draft, caret, uploads and send handlers. Without the opt-in, growth is unchanged.
Use `--composer-wrap: 0` at an inactive responsive breakpoint: normal growth remains active, while width observation
continues so switching back to `1` reflows the existing draft without another keystroke.

`--composer-wrap-right` is the extra right inset of the full-width text beyond the form's padding;
`--composer-wrap-gap` is the gap between the narrow text and control corner. Both are pixel lengths, defaulting to zero.
The core measures the narrow width even when deciding to stack and reflows after the form width changes. Recording
and forms with `.has-leading` keep their specialized layout. No DOM observer or private core import is needed in a plugin.

### The feed's loading stub (1.34)

While a session's feed has not arrived yet, the chat shows one row "Loading the feed…": a `p.hint.feed-loading` inside
`main .feed`. A theme or plugin that restyles or hides it matches the `feed-loading` class, not the text (it is in the
page's language). Older cores have only `hint` on it.

## Marketplace plugins: install others', publish your own

A **plugin** is a public GitHub repo with a `hub-plugin.json` file in the root:

```json
{"name": "mascot", "icon": "icon.svg", "description": "An owl in the sidebar footer shows the state of the fleet",
 "why": "Shows whether the fleet needs you, without reading the session list", "version": "1.0.0", "hub": "^1.0", "web": "main.js"}
```

Every plugin has `icon`, `description` and `why`: an icon (a path to an svg, png or webp in the repo, square, up to 256 KB),
what it does and why. They are visible in the "Plugins" catalog; what is missing is stated on the card ("The author gave no
icon…"), and the plugin is installed but looks unfinished. `web` is a page module, `run` is a process next to the core
(`["python3", "main.py"]`), `themes` are themes, and `skills: true` means the repo is also a skill set
(`.claude-plugin/marketplace.json`). At least one is required; the name is Latin letters, digits and hyphens.

**Catalogs (marketplaces).** A catalog is a git repo with a `hub-marketplace.json` file in the root: a list of entries,
each pinned to a commit (`sha`), so exactly that commit is installed, not whatever the author has now. The hub connects the
official `agents-hub` catalog by itself; connect your own or someone else's with `hub marketplace add <owner/repo | git address | folder>` (someone else's catalog is someone else's code on your machine with your access). What they contain is
in Settings → Plugins or `hub marketplace list|search`; to install or update to the catalog's version use "Install" /
"Update" (`hub marketplace install <entry>[@<catalog>]`), and to remove use `hub marketplace uninstall <key>`; catalogs
refresh themselves once a day (`hub marketplace update`), and only the owner installs. An entry:

```json
{"name": "kanban", "type": "plugin", "source": {"github": "acme/hub-plugin-kanban", "sha": "<40 characters>"},
 "version": "1.2.0", "description": "A task board", "category": "work", "icon": "icons/kanban.svg",
 "rights": {"page": true, "process": "python3 main.py"}}
```

`source` is `"./path"` (a folder in the catalog repo), `{"github": "owner/repo", "sha", "subdir"?}` or `{"git": "<address>", "sha", "subdir"?}`; `type: "module"` is a core module (its `rights` is `{"capabilities": [...], "env": [...]}`). `rights` states the rights in the catalog's words, for the list: the hub checks them against the manifest at the
commit, and code that asks for more than promised is not installed. To check your catalog before publishing, use
`hub marketplace validate <folder>`.

**What was installed before catalogs** (`hub market`, folders `~/.hub-core/market/…`) keeps working as installed. If its repo
is pinned in a connected catalog, it counts as "from the catalog": "Update" installs the catalog's version into new storage,
and the key and your plugin settings stay. Otherwise it is outside catalogs and is updated only through review (below).
`hub market search|list|install|update|remove` are synonyms of `hub marketplace` until Plugin API 2.0 and print the new
command. The hub has no plugin list of its own besides catalogs, and a GitHub account is not a marketplace: without a
catalog the author's commit is pinned by nothing, so such a plugin goes by link.

**By link** means any public GitHub repo with `hub-plugin.json`, without a marketplace: paste the link into the field in
Settings → Plugins or give it to the hub (`hub market review <link>`). Such a plugin is installed only through review: Hub
Developer reads the code at the commit, the verdict is visible on the card, and exactly the reviewed clone is installed
(`hub market install <account>/<repo> --reviewed`).

**Review and install** is how to install someone else's code: the hub clones the plugin at the commit into a review folder
(nothing is connected), Hub Developer reads the code (what it runs, where it connects, what it reads and writes, whether it
touches keys) and issues a verdict (`hub market verdict <repo> ok|risky|bad "<what was found>"`). The verdict is on the
card; "Install the checked version" installs exactly this clone: if the author changed something after the review, it does
not get in.

An installation is a commit folder in `~/.hub-core/plugins/` and a line in `hub.json` with the source. A plugin is named with
its author: `<account>:<name>` (`acme:kanban`; not from GitHub: `<catalog>:<name>`): identical names from different authors
and your own personal extensions do not interfere with each other. New versions come only via "Update"; to turn one off
without deleting, use Settings → Plugins; to remove, use "Remove" (`hub marketplace uninstall <key>`). Someone else's code
runs on your machine with your access: install what you trust.

**Rights and updates.** The hub remembers what you allowed a plugin to do (`approved` in its `hub.json` entry): page code,
a process with the exact `run` command, themes, a skill set and everything executable in it: hooks, MCP and LSP servers,
monitors, `bin/` files. An update that asks for more (a new process or a different command for it, page code, a new hook, a
different MCP server command under the same name) does not install by itself: the previous version keeps working, and the
page shows exactly what the new one asks for and an "Allow and update" button (`--accept` on the command line). New code with
the same rights, and removed rights, are an ordinary "Update". Every consent is a `plugins.consented` event in the journal
and a message in Telegram.

**Page code has full access to the page** (`"page_access": "full"` in `hub-plugin.json` is a declaration, not a permission: a
plugin without the field gets the same). There is no isolation: page code can do everything the owner can do from the page:
write to sessions, answer permissions, install plugins and press "Allow and update" itself; that is why the consent is
visible in the journal and in Telegram. Settings → Plugins, the catalog and the install output always say so.

**Revocation.** A catalog entry that has been revoked (`revoked`) is installed only with `--despite-revoked`; the official
catalog switches off anything installed from a revoked entry by itself and records the reason, while a third-party catalog
only warns.

To publish your own: a repo `hub-plugin-<name>` with `hub-plugin.json` and an entry in a catalog pinned to its commit (a PR
to the catalog repo), or your own catalog: `hub-marketplace.json` in your own repo, and colleagues run
`hub marketplace add <owner/repo>`.

## Your own client: a phone app, a script

A client outside the page uses the same API as the page and does not need one of its own. The owner is let in: localhost or
the owner's device in the tailnet; every POST carries the `X-Hub: 1` header. While the page has only just started and is still
reading the journal (or rereading it from the beginning), the answers that need the core state (`/api/timeline`,
`/api/history…`, `/api/feed`, `/api/flow`, `/api/usage`, `/api/modules`, `/api/subagent`, `/api/send/receipt`, a POST with
a session while it is not yet in the empty snapshot) are 503 with `Retry-After` at once: repeat later; if the journal lock is
held by polling for longer than 15 s, that is also 503 (except `/api/feed`: since 1.31 it does not wait for the lock and
answers with the posts of the latest read-through, like `/api/snapshot`); `/api/usage`, while "Usage" reads the journal archives for the first
time (after an update, minutes), waits up to 10 s and then answers 503 with `Retry-After`. `/api/snapshot`, `/api/stream` and
`/api/about` answer with the latest read-through state (until the first read-through the snapshot is empty, and in
`/api/about` `core_started` and `sessions` are `null`). If the page has more connections than the limit (`core/httpd.py`),
it answers 503 at once and closes; a connection silent for 60 s is closed (with keep-alive, open a new one).

- `GET /api/stream` is an event stream (SSE, gzip-compressed): first a `snapshot`, the whole hub state (sessions and their
  open questions `pending`, messages, the schedule, limits, `extensions`), then `patch`, only the changed top-level keys
  (`_gone` for the vanished ones); a ping comment every 15 s. A session's `background` holds its background commands and
  agents (`kind`: `bash`, `agent`) and, since 1.36, its `hub run` commands (`kind: "run"`, with `until_at`; they run outside
  the session, so a parked session shows them too).
- `GET /api/stream?delta=2` (API 1.36, `stream.delta2` in `features`) is the same stream with smaller patches: a changed big section
  that has keys — a dictionary (`messages`) or a list of objects with a unique string `id` or `name` (`sessions` by
  `name`, `schedule` by `id`) — comes as its changed items under `_delta`, not whole. The `snapshot` event is the same.
  A patch:

  ```json
  {"seq": 812, "clock": {"state": "ok"},
   "_delta": {"sessions": {"by": "name", "set": [{"name": "maps", "state": "working"}], "del": ["old"],
                           "order": ["maps", "hub", "atlas"]},
              "messages": {"set": {"m7": {"id": "m7", "text": "hi"}}, "del": ["m1"]}},
   "_gone": ["voice"], "_n": 3}
  ```

  Apply it in this order. (1) `_n` numbers the patches of this connection from 1 and is in every patch of such a
  stream; it must be the previous one plus 1 (1 after the snapshot). Any other number, or a `_delta` with no `_n`,
  means the client's state is not the one the patch was made for (its reader dropped a frame): drop the patch, close
  the stream and open it again — it starts with a full snapshot and the count starts over; the same when a `_delta`
  names a section you do not hold or of another kind, or its `order` is not exactly the keys you hold. (2) Every other
  top-level key replaces that section whole, `_gone` removes sections — as in a plain patch. (3) For each section in `_delta`: with `by` the section is a list and an
  item's key is `item[by]`, without it a dictionary; remove the keys in `del`; put each item of `set` (always the whole
  item, never a part of it) in place of the one with the same key — it keeps its position — and a new key at the end, in
  the order of `set`; then, if `order` is there, line the items up by it: it names every key of the section, once.
  Without `order` the positions after `del` and `set` are already right. The server chooses per patch: a small
  section (about 2 KB of JSON), one without keys, or one whose delta would not be shorter comes whole as a top-level
  key, so handle both forms for any section, and do not keep a list of sections — `by` says what the keys are. A
  client that ships apart from the hub asks for `delta=2` only when `features` has `stream.delta2` (a hub without it
  ignores the parameter and sends plain patches, which the same rules apply as they are). Without `delta=2` (and with
  any other value: the apps of earlier versions send `delta=1`) patches are whole sections, with no `_delta` and no `_n`.
- `GET /api/timeline?session=<name>&since=<rev>&epoch=<epoch>` is a session's feed: the rows changed after revision `since`
  (in the snapshot, `timeline_rev[name]` and `epoch`); merge by the row key `k`; `full: true` means the feed was sent whole.
  Service rows for the developer (the `hub wait` steps, "session created", "brought up again" after parking, etc.) are not in
  the feed, neither here nor in the snapshot and `/api/history`; they are in the journal and `hub show`. An `out` line with
  the text "—" is a quiet turn result: it arrives but should not be shown (the turn has no reply; the page does not draw it).
  An `in` row with `relayed` (API 1.31) is the owner's words to a mini-hub's executor that went to its mini-hub instead
  (`relayed` is the mini-hub's name; the same message `id` is in the mini-hub's feed with the "Owner to executor …" text):
  the executor did not get it, so it is not its queue and starts no turn there (`midturn`: its turn was running) — show
  it where it was written, with "passed to <mini-hub>" instead of "queued"/"delivered" (a failure is still worth showing).
  An `in` row with `addressed` (API 1.36) is the owner's words to a session that is gone (removed, closed, failed) or never
  was: the row is in the feed of whoever got them (the hub; for a removed executor, whoever raised it), `addressed` is the
  name he wrote to — show "to <addressed>, passed on". `POST /api/send` to a session missing from the snapshot answers
  202 when the name is session-shaped (lowercase letters, digits, `.`, `_`, `-`, up to 40) and the core passes the words on
  so; any other name is 404 as before. A hub of an earlier version answers 404.
  An `in` row of a member of the hub's Telegram group (API 1.37; `from` `{kind: "member"}`, a core before has only that) has
  `member` `{name}`: a person who only asks, not the owner — draw the words as another person's (by the name; with no
  `member` — by the role), not as his own bubble, and offer neither "Send again" nor a reaction on them. An admin's words
  are the owner's and have no `member`.
  An `in` row of a task (`task: true`, next to `words` and `context`) may have `brief` (API 1.36): `[start, end)` in UTF-16
  units (what `String.slice` counts) of `text`, where the words of whoever set the task lie. The rest of `text` is the
  core's frame for the model (the rules an executor gets, the direction's note, the task number) — show the words and fold
  the frame ("for the model only"); the core marks them itself, so do not look for the border in the text. No `brief`
  (a task of an earlier version, or the text no longer holds the words): show `text` whole, as before.
- `GET /api/history?session=<name>&before=<seq>&n=<up to 500>` is the feed's past older than the event `before` (the core's
  live feed, then the journal archives): `{items, more, building}`; with `after=<seq>` instead of `before`, up to `n` rows
  newer than `after` (a window around something found by search): `{items, building}`.
- `GET /api/history/search?session=<name>&q=<text>&before=<seq>[&cursor=<archive>][&tab=<id>]` is a text search in the feed's
  past older than `before` (the page passes the number of the earliest loaded one), without a model: case-insensitive, the Cyrillic
  yo = ye, in the row fields from the snapshot's `search_fields` (row kind to fields; the page searches what it has loaded by
  the same list). The response is one batch: `{hits: [{key, seq, ts, kind, id, snippet}], cursor, done, capped, left}`; if not
  `done`, the same request with the `cursor` gives the next one. `key` is the row key, like `seq + kind + id`; one row in
  different batches should be merged by it. `building` means the archive on the way is still being parsed (repeat with the
  same `cursor`), `unparsed` means parsing failed and nothing beyond this event number was searched, `failed`/`unavailable`
  means the search failed or did not start, and `cancelled` means a new search from the same tab `tab` superseded it. A
  query shorter than 2 characters is 400.
- `GET /api/session/kit?session=<name>&days=7|30|all` (API 1.37) is what a session's process loaded and what it really
  used: `{session, days, kit, mcp_status, lead?, used: {own, executors, executors_count}, complete, period_ready,
  read_from, files_from, volume}`. `kit` is the set its engine reported last (`null` — not yet): `skills` `[{name, source
  (plugin | repo | built-in | user | …), plugin?, path?, in_prompt?, tokens?}]` (`in_prompt: false` — Claude Code does not list it
  to the model, e.g. `disable-model-invocation`; absent — not known; `tokens` — what its entry takes of the model's
  context, as Claude Code counts it), `listed` `{in_prompt, total, tokens?}` (`tokens` the whole list's, `null` or absent
  — the engine does not say, as Codex), `plugin_errors`,
  `mcp` `[{name, tools}]`, `engine`, `at`. `mcp_status` is `{server: connected | failed | needs-auth | disabled |
  pending}` now. `used` maps `skill:<name>` and `mcp:<server>/<tool>` to `{tool, command, file, failed, unknown, convs, last,
  this_conversation?, args?, unverified?}`: calls within the period (`tool` a step, `command` a `/<skill>` that opened a
  turn, `file` a successful read of its SKILL.md), `last` over all the history read; `args` splits an MCP tool's calls
  by the argument that names what it opened (`name=…`); `convs` is in how many conversations of the period it was used.
  `volume` `{own, executors}`, each `{conversations, turns}`, is what happened in the same period — the denominator of
  those counts (conversations that had a turn; turns). `lead` (an executor) `{name, missing}` is the skills its lead
  has and it does not. Until the archives are digested (the first ask starts it) `complete` is false and `read_from`
  says from when the history is read.
- Actions (POST, JSON): `/api/send` `{session, text, attachments, quote, card}` is a message (with `quote` it is a reply to a
  line; with `card: "qN"` it is an answer to that session question card: another session's, a closed or a stale `qN` still has
  its words delivered to the session, but they do not become an answer to another card or form; without `card` the words
  answer a card only if it is the only open one, and with two or more open they answer none (API 1.30; before, the last
  one); API 1.22; with `client_id`, a string of up to 128 characters, the message id on the client's side, API 1.25: a
  repeat to the same session with the same id within a day is 202 again, and the session gets one message: the core rejects
  the second, with `core.rejected` in the journal carrying the number of the first `mN`; `null` is as without the field, a
  non-string or longer is 400; API 1.29 adds a receipt: the response waits for the core's decision for up to 3 s: 202 `{ok, receipt: "accepted", seq, id, client_id?}` (`id` is `mN` or `null`; a repeat of an accepted one comes with `already: true`,
  the first one's `seq` and `id`, and is not put into the spool a second time, even if the session no longer exists), a core
  refusal is 409 `{ok: false, receipt: "refused", error, code?, seq, client_id?}`, and if it has not decided, 202 `{ok, receipt: "queued", queued: true, text, client_id?}`; the outcome later is `GET /api/send/receipt?session=<name>&client_id=<id>` → 200 `{receipt: "accepted", seq, id}` | `{receipt: "pending"}` (in the
  spool) | `{receipt: "refused", error, code?, seq}` | `{receipt: "unknown"}` (the core did not take it, and it is not in the
  spool), and 400 if `session` or `client_id` is missing; with `todo`, a string of up to 128 characters, the id of an item in
  the client's to-do list that the owner answered in words, API 1.25: the core puts it into the `message.received` data as is
  and does not interpret it, since a module reads the journal; the `null` and 400 rules are as for `client_id`; `ref` is the
  same for a client's reference or note, API 1.36: a string of up to 128 characters in the `message.received` data as is, so
  that a module can show the reply and the session's answer as a thread under that note; it is separate from `todo`). An attachment
  is only a file uploaded through `/api/upload` or `/api/upload/raw`: any other path is 400 with a reason, and the message is
  not sent (before 1.22 such an attachment was silently dropped, and the response was 202). `/api/upload` is an attachment,
  `/api/answer` and `/api/permission` are an answer to a question and a permission (`/api/question/dismiss` closes a question
  without an answer; API 1.28: for all three `client_id` is a string of up to 128 characters, the answer's id on the client's
  side, and the core puts it into the fact; the receipt is 202 `{ok, seq}`, the number of the accepted fact, and whether it was
  applied is in the session's `answered` in the snapshot, `[{request_id, seq, sig, client_id?}]`, the latest closed cards; a
  repeat on an already closed card with the same decision or the same `client_id` is 200 `{ok, already: true, seq}` of that
  fact, with no second fact; with a different decision it is 409 `{error, reason: "answered_other", seq}`; no such card at all
  is 409 as before; API 1.36: a record of `answered` also says how the card was closed — `how` (`answered`; `dismissed`,
  the owner's ×; `withdrawn`, by the session that asked, the hub or the owner), `id` (the card's `qN`, `null` for a permission
  form), `ts`, `by` (`"owner"` or the name of the session or the hub that closed it), `why` (the words a withdrawal came with,
  up to 300 characters, or `null`); a withdrawn card has `sig` `null`, and a reply to it is 409 as to a card that is not there,
  not "already accepted" (no reply of the owner was applied); records written by an earlier core lack the new keys; 16 latest
  per session. API 1.30: if the core did not record the answer within 3 s, 202 `{ok, queued: true, text}`, and the client
  asks for the outcome with the same request and the same `client_id`: the fact is still in the spool, so `queued` again, with
  no second fact; the question is open but the core rejected this answer: 409 `{error, reason: "refused", seq}`, and there is
  no point repeating the same answer (a new answer takes a new `client_id`); the journal reader is busy: 503, ask later),
  `/api/queue/cancel`, `/api/session/model`, `/api/session/effort` `{session, effort}` (or `level`, as before; the level is
  the session's `effort` in the snapshot, `null` means the default, API 1.22; `default` clears your own level, API 1.25),
  `/api/feed/post` `{plugin, text, reply_to}`, a post to the plugin channel (up to 20,000 characters, longer is 400; before
  1.22, 2,000 with silent truncation), `/api/schedule/remove`; `GET /api/file?path=` is an attachment or a file from the feed:
  an owner's upload, an image the session showed, and a file it sent (`hub send-file`, the `file` feed row), since API 1.25 of
  any type (before that, only images and video). Raster images, audio and video are `inline`, and everything else (html, svg,
  PDF, text, archive) is download only (`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, CSP `sandbox`):
  it is not executed from the hub's address.
- Core commands via "/" (API 1.25): `commands.list` in the snapshot has the same ones as the page (`/compact`, `/reload`,
  `/model`, `/effort`, `/stop`, `/away`, `/back`, `/restart`), each `{name, args, hint, path, session, arg, choices?}`: to run
  one, `POST path`, with `session: true` meaning `session` in the body is the session name; the argument goes in the `arg`
  field (`hours` is a number, `parts` is a list, the rest a string); `args` in `<…>` means the argument is required, in `[…]`
  not; `choices` is the name of a neighboring list (`commands.models`, `commands.efforts`) of allowed values. The response is
  as for the buttons: 202 (`{queued: true, text}`: the core has not recorded it yet), and a core refusal is 409 with a
  reason. Anything not in the list goes to the session as text.
- A hub entirely on Codex (API 1.34, EXPERIMENT: hub.json `"host": "codex"`, not on main): `commands` in the snapshot has
  `codex: true`; `commands.models` are `codex/<id>` (what Codex lists, read without a model call) plus `default`,
  `commands.titles` is `{codex/<id>: name}` for the menu, `commands.model_efforts` is `{codex/<id> | "default": [levels]}`
  — the levels of `commands.efforts` that model knows (a model not there takes them all; `default` — the hub's model's).
  `/api/usage` keeps `unit` as the main unit (`usd` | `tokens`); with both Claude dollars and Codex tokens in the shown
  days, `other` is the other unit's part of the same form (`{unit, days, windows, status, top}`). `GET /api/setup` lists
  `codex` and `codex_login` instead of `claude` and `login`. Any hub with Codex sessions (`HUB_CODEX=1` too) may also give
  recap rows by `codex`, permission cards with tool `Permissions` (`{permissions, cwd}`) and `/api/subagent` answers with
  `agent.type` `codex`. A hub without Codex sessions answers as in 1.33.
- Attaching a file (API 1.22): `POST /api/upload/raw?name=<name>&mime=<type>`, with the body being the file itself (not
  JSON), `Content-Length` required (411 without it), up to the snapshot's `upload.max_bytes` (200 MB by default,
  `web.upload_max_mb`; larger is 413); the response is `{path, name, mime, size}`, as for `/api/upload`, and `path` goes into
  the message's `attachments`. The hub supports this if the snapshot has `upload` (`raw` is the path, `max_bytes`,
  `max_inline` is the limit of `/api/upload`) and `upload.raw` in `features`; without them, use `/api/upload` `{name, mime, data}` (base64, up to 20 MB). A drop in the middle of the body means there will be no file.
- The hub's own addresses (API 1.32, `about.addresses` in `features`): `GET /api/about` → `addresses: [{kind: "ip" | "name", url}]` — the tailnet IP (`http://<ip>:8787`) and the node's tailnet name (`http://<name>:8787`). A client keeps the whole list as fallbacks and tries them in turn: the IP changes when Tailscale is reinstalled on the Mac, the name does not. The list is empty while the tailnet is off. The module also writes `urls_dns` (the same as `urls`, by name) to tailnet.json.
- Choosing a Claude account (API 1.30, `accounts.use` in `features`): `POST /api/accounts/use {account: <name from the pool>, force?: true}` → 200 `{ok, active}`; 404 means it is not in the pool; 409 `{error, code: paused | busy, why?, until?, forceable?}` (API 1.36: `code`, tell the refusal by it, not by the text; `why` — the cause alone in the hub's language, the window the account waits for: "5-hour limit spent" / "weekly limit spent" at 100%, otherwise its share or the old reason, so a client builds "busy until 15:17 — <why>"; a sign-in module's own `why` as is) means the
  accounts are paused or the account is busy (a limit, the switch threshold, an API refusal), and without `force` it is
  refused with a reason and the reset time; 503 means a sign-in module runs the sign-in (Module API 2.8) and did not answer in
  time: nothing was recorded, repeat later; `forceable: true` means `force` will help. `force: true` bypasses only the switch
  threshold: the account is held until the API really refuses; for one the API has already refused (a reset is ahead),
  `force` does not help (409, `forceable: false`); `force` is only `true` (the string `"true"` is as without it). The active
  one is the snapshot's `account.active`, and `account.forced` means it was chosen with `force`. The earlier `name` field is
  accepted.
- Refreshing an account's limits (API 1.34, an `accounts.pool` entry with `probe: true`): `POST /api/accounts/probe
  {account?: <name from the pool>}` = `hub accounts probe [<name>]` — a fresh reading of the 5-hour and weekly limits
  without a switch: one short request with the account's own token, the limits from its response headers go to the core
  as `account.limits` with the account and its token generation, as the gateway's do, and arrive in the snapshot's
  `account.seen`. Without `account` — every account with its own token (a token by command and the Claude Code login are
  not measured: no `probe`). While a sign-in module leads session login, the module measures its own pool and writes the
  readings (an older module without the measurement — 409 asking to update it). On an account that is not the active one the reading opens its 5-hour window — its reset
  moves to 5 hours after the reading; every reading spends a little of the account's limit. → 200 `{results: [{name, ok,
  error?}]}` (`error` — why that account was not measured, in the page language); 404 — not in the pool; 409 `{error}` — the
  sign-in module refused or cannot measure, another reading is running, or there is nothing to measure.
- A client learns about page plugin sections the same way the page does: the snapshot's `extensions` and their
  `/ext/<name>/<main>` (for a core module's plugin, `base` + `main`, API 1.21).
- Core modules (API 1.21): while at least one is loaded, the snapshot has `mods`, their state `{module: {key: {value, rev}}}`,
  `mod_versions` is `{module: version}`, and `modules` is in `features`; without modules these keys are absent ("Core modules:
  own state and a page plugin").
- `features` in the snapshot are optional on this hub; a client does not offer what is not in the list. `push` is push
  notifications about questions to the owner (to the phone): the module is off by default (the switch in Settings → Modules
  or `push.enabled`), the APNs key of your own app goes in `hub.json` (`"push": {"enabled": true, "key_file", "key_id", "team_id", "topic"}`), and the client sends the device token with `POST /api/push/register` `{token, env, mode, caps}` (up
  to 20 devices; `mode`, since API 1.25, is the device's level: `none` means nothing for it; `asks`, `results` and anything
  unfamiliar mean questions, tool requests, reminders and urgent notifications; `all` adds all `owner.notify`
  notifications; without the field or `null` it is the level sent earlier for this token, and `asks` for a new one). While
  it is off or has no key, nothing goes anywhere.
  `push.alerts` (API 1.26) is the removal of banners. The reason for a push is a question, a permission, a tool request or a
  notification; the snapshot's `alerts` holds the open ones `[{key, id, kind, session?, card?, title, subtitle?, body, since, v, quiet?}]` (`quiet: true` — a reason held for the working morning by `quiet_review` in hub.json, its `since` is the release; `kind` is `question`, `tool`, `notice`, `batch`; when `v` grows, it is the same banner with a new text, for example a
  reminder) and `alerts_rev`. A banner arrives with `apns-collapse-id` = `key` (the same reason replaces rather than adds)
  and a `hub` field `{v: 1, hub, rev, key, kind, card?, n?, closed, open?}` next to `aps`; `aps.alert` has `title` (the
  topic), `subtitle` (the session) and `body`. When a reason is closed (an answer from any device, the question withdrawn,
  the session deleted, `owner.notify_resolved`), a device with `caps: ["clear"]` gets a silent push (`content-available`, no
  more than 3 per hour) `{hub: {v, hub, rev, closed, open?}}`. The client removes, among the shown notifications with the same
  `hub.hub`, the keys from `closed` and, if `open` is present, all those that are not in `open`; it does not apply a push
  whose `rev` is lower than one already seen. With more than 60 open, `open` is not sent: remove only by `closed`. Apple does
  not guarantee a silent push and it does not wake an app closed by swiping, so the main path is reconciling with `alerts` when
  the app opens; a banner can hang until then. More than 3 new reasons at once give 2 banners and a `batch` reason "N more".
  `reactions` is the "Reactions" module (off by default, `reactions.enabled`): the snapshot's `reactions` key holds `badges`
  (`mN` → the icon at the owner's message: 👀 taken, 👍 answered, the owner's own, or empty if the owner removed it
  themselves; a session feed
  row's event number → the owner's reaction) and `taken` (session → the number of the turn event begun by the owner's latest word:
  the rows before it are answered); a reaction is `POST /api/react` `{session, target, emoji, quote}` (an empty `emoji`
  removes it). Without the module `reactions` has only `taken` (folding what was answered is up to the client; on the page it
  is the "Brief" checkbox), and `/api/react` is 409.
- `waiting` on a session in the snapshot is what it is waiting for now (`hub waiting`: `text`, `ts`); if after that someone
  wrote to it (not a core service message and not the schedule) and it did not repeat, `waiting` is empty, and the same words
  are in `waited` (with `stale`, when someone wrote): show it as "was waiting", not "is waiting" (API 1.15).
- `status` on a session in the snapshot is a summary status by a single core rule (API 1.17), for the "waits for you" labels,
  groups and counters. The rules go in order, and the first one decides: `failed` means it crashed; `asleep` means it is
  closed (by `state`, even with questions in `pending`); `waiting` means the owner has something to answer (a question as a
  form or card in `pending`, a tool request `needs-owner`, only for a live one: the core does not accept a tool decision for a
  sleeping or parked one), and a question counts for a sleeping one too: an answer will bring it up; `working` means a turn is
  running, starting, free with background work, or a Claude Code window is open without a form (`terminal`); `idle` means
  free, parked, in manual mode (`held`: the core does not see its turns in the terminal) or not yet brought up; `asleep` means
  sleeping. The watchdog's guess (`attention`) and `waiting` are not `status`, and have their own fields. Treat an unfamiliar
  value (from future versions) as `idle`; a hub of an earlier version (API below 1.17) has no field. The row icon in the list
  follows `status` (manual mode `held` has its own icon); whether a turn is running follows `state`: for a working session with
  an open card the turn is running, while `status` is `waiting`.
- `out_head` on a session in the snapshot (API 1.24) is the beginning of the latest result addressed to the owner (the same
  result that lights `unread`): up to 160 characters on one line, truncated with "…" if longer, without Markdown marks (the core strips `**`, `*`, `` ` ``, links, heading/quote/list marks; as is before API 1.32); `null` means
  there has been no such result yet (and for a hub of an earlier version). On the page it is the second line of the session in
  the "New" section of the "Now" tab.
- `out_head_ts` on a session in the snapshot (API 1.31) is the time (event `ts`, seconds) of the same result as `out_head`:
  compare the result with other timed facts (such as `gist.ts`) by it instead of by `since`. `null` means there has been
  no result yet; a hub of an earlier version has no such field, and for a result that arrived before the update it is `null`
  until the next result to the owner.
- `stops` on the agent-training session (the one with `trains`, API 1.31; other sessions have no such key) is the stops
  audit of the agents — the system job `agent-dev-stops`: `{ran, check, done, asked, streak, alarm}`. `ran` — the last
  precheck tick (or when the job was set), `check` — its code (0 nothing due, 3 due, another — an error), `done` — the
  first quiet tick after due ones (the audit was marked), `asked` — since when a due audit is not marked (`null` — none),
  `streak` — due ticks in a row, `alarm` — `"off"` (no active job), `"silent"` (no tick for 2 days), `"failed"`,
  `"skipped"` (three due ticks in a row) or `null`.
- `stopped` on a session in the snapshot (API 1.31) is `{ts, by}` for an executor stopped at once (`hub session stop`,
  the page's "Stop executor"): `ts` is when, `by` is `"owner"` or the name of the session that stopped it (the hub or
  whoever raised it). It stays while the executor sleeps (`state` `asleep`, `retired` true) and is gone once it is woken;
  `null` otherwise, and for a hub of an earlier version. Show "stopped by you · 13:02" instead of "handed in". To stop an
  executor from a client — `POST /api/session/stop` `{session}`: the core kills its process tree at once, even mid-step,
  withdraws the lines it has not read yet (their messages become `failed` with `reason` `"stopped by owner"`) and puts
  it to sleep with its conversation; the owner's message to it wakes it. Only an executor (`role` `executor`, no
  `backlog`) — anything else is 409, and so is an executor that is not running.
- A picture's thumbnail and revalidation (API 1.36, `file.thumb` in `features`) — `GET /api/file?path=…&w=<px>` answers
  a raster picture (png, jpeg, webp, bmp, tiff, heic — judged by its bytes, not its name) scaled by width as a jpeg
  (`Content-Type: image/jpeg`, the name ends in `.jpg`), or (API 1.37) as a png (`image/png`, `.png`) when the picture
  has an alpha channel (a window screenshot with its shadow keeps its transparency), kept in the Files module's private cache. `w` is rounded
  up to 120, 240, 480, 960, 1920 or 2048. The original is served instead (check the `Content-Type`) when the picture is
  not wider than `w` (it is never enlarged), has over 64 Mpx, is not a raster picture, the machine has no `sips` (macOS
  only), or both scalers stay busy for 2 s. `X-Hub-Thumb` (API 1.37) says what came: the copy's width (the rounded `w`), `0` for
  the original; on a 304 too. Every `/api/file` answer carries an `ETag` (strong; a thumbnail's is weak, W/, of its
  source and width, ending in `-w<px>`: a thumbnail always comes whole, Range or not); `If-None-Match` with it is a 304
  with no body. `Cache-Control` of `/api/file` is `private, no-cache` (it was `no-store`): revalidate each time, never in a
  shared cache.
- "Continue" on a session paused by an account limit (API 1.36, `session.resume` in `features`) —
  `POST /api/session/resume` `{session}`: the core continues the turn the limit cut off now, the same way it continues
  it by itself once the limit window resets (a core message in the cut turn's thread; with an account pool, on the
  active account). 202 — continued: the new turn ends the pause as any turn does. 409 `{error, code: "not_paused"}` — no
  turn cut by a limit, or a message to the session is already on its way (its turn ends the pause); 409
  `{error, code: "not_yet", until}` — the window still refuses (a Claude window counts until 15 s after its reset, as
  the core's own clock waits): `until` (Unix seconds, `null` if unknown) is the earliest the session continues by
  itself, `error` names that time in the owner's zone. 409 `{error}` without `code` — the session is not running: asleep
  (it continues with the next message to it), in the owner's terminal, crashed, closed or removed (the core's refusal by
  state). A hub of an earlier version has no such path (404); there a reload (`POST /api/session/reload`) restarts the
  process and does not continue the turn.
- `relay` on a session in the snapshot (API 1.31) is the mini-hub the owner's words to this executor go to (the bus: the
  owner talks to the mini-hub, which passes his words on); the input field should say so ("goes to <mini-hub>, it passes
  it on") instead of "goes out after the turn". A wake of an executor that handed in, a Claude Code command and an answer
  to its open card still go to the executor itself. `null` — the words go to the session itself, and for a hub of an
  earlier version.
- `inserted_unread` on a message in the snapshot (`messages[id]`, API 1.31) is true for a line typed into a busy session in the
  middle of its turn (`hub send --now`, the watchdog) that no turn with its mark has started on yet: Claude Code reads
  stdin only between steps, so during a long step the line waits. `state` stays `delivered` (it is in the session's
  input), and `label` is "inserted, not read yet" instead of "delivered". The core sees the reading only as a turn with
  the message's mark; a turn that ends without one read it inside, so `inserted_unread` goes with the end of the turn and with a
  new process. The page shows the step that was already running when the line was typed (its timeline row without a result) and, past a minute, a row
  with "Stop executor" (`POST /api/session/stop`) or "Interrupt turn" (`POST /api/interrupt`) while the session is `busy`. Not to be confused
  with `unread` on a session (results the owner has not seen). `false` otherwise; a hub of
  an earlier version has no such field.
- Board cards in a message (API 1.32, `send.tasks` in `features`): `POST /api/send` takes `tasks`, a list of up to 40
  `{id: "TASK-N", title, status}` — the cards of a direction's board (`GET /api/backlog?slug=`) the owner named in this message.
  The core cuts titles and statuses to one line, drops what is not a `TASK-N` id and repeats, and puts one line into the
  label the session reads ("about board tasks (as on the page): TASK-2 «title» (status); …"); the owner's text stays
  his own words. A message from anyone but the owner ignores the field. An `in` row of a timeline (`kind: "in"`) has the
  same cleaned list as `tasks` when there is one: draw it as chips under the words, and send it again with "Send again".
  A hub without `send.tasks` in `features` drops the field silently — such a client puts a line with the cards in front
  of the text instead (the page does).
- The client in the owner's label (API 1.32, `send.app` in `features`): `POST /api/send` takes `app` (`web`, `ios`, `mac`,
  `android`, `telegram`) and `app_version` (up to 20 ASCII letters, digits, spaces, `.`, `+`, `(`, `)`, `-`, `_`, e.g.
  `2.14 (412)`). The session reads the message as `[hub:mN from user · Android 2.14 (412)]`: the author is still the
  owner, the tail only names where the words came from. An unknown `app`, a version with anything else, or a version
  without `app` is a 400 with the reason in `error`, and nothing is sent. Only a client that names itself sends them: the page
  and the Telegram bot do not, their label stays as it was. A hub without `send.app` in `features` ignores both fields.
- `news: true` on a feed row `out` (API 1.32) is a result that the core counts as new for the owner (the same one as `out_head`
  and `unread`: an answer to the owner, not an answer to the hub or a result sent to another session). The page counts
  "New · N" from such rows and places the "Last read" line; a plugin that draws its own "new" mark should take them too, not
  any result after `seen`. For a hub of an earlier version and for old records the field is absent — then any result after
  `seen`.
- `gist` on a session in the snapshot (API 1.25) is its essence in one line, which it writes itself, as the first line of a
  long result or with the command `hub gist`: what it did, what it needs from the owner, what it is doing; `{text, ts}`, up
  to 160 characters (the core cuts longer ones at a word, with "…"). When there is no essence of its own or it is stale
  (the latest result to the owner came from a turn where the session did not update the essence, or it came later in the
  same turn than the `hub gist` said before it and has no gist line of its own), `gist` is the start of that result (API
  1.33), `{text, ts, lead: true}`: a short result whole, a cut one by its first sentence, `ts` is the time of the result.
  The exception: the session still waits (`waiting`) or runs a background job of its own that is not a service one, and
  then its own essence stays. Without an essence of its own `null` means there was no result to the owner at all or its
  latest output went to another session rather than the owner (the result to the owner is an old one): show nothing, not a
  goal. A client that took `null` as "show out_head" now gets that text in `gist`; `lead` tells it from the session's own
  essence. A turn with no result to the owner (`—`, a built-in command) changes nothing. For a turn in progress it is an
  essence stated no earlier than its start (`ts` ≥ the session's `since`).
- `terminal` on a session in the snapshot (API 1.17) means a Claude Code window is open and there is no turn form (the tmux
  host sees a window only through the Claude Code registry): an answer is possible only in the terminal, `hub attach <name>`;
  if there is also a question or a tool request, `status` is `waiting`, and it is worth showing the terminal hint next to it.
- Mini-hubs (API 1.18): a session's `minihub` is a mini-hub (it assigns tasks to its executors), `minihub_pending` means it
  will become one (what it is waiting for), `solo` means "on its own" (since 1.23 the same as `parallel: 0`), and
  `solo_auto` means "on its own" automatically (why). Since 1.23 a session's `parallel` is its own number of executors for
  a specialist or direction (`0` means on its own, `null` the default), `executors_live` is how many places its live
  executors occupy, and `parallel` at the snapshot's root is the hub's overall parallelism; to set it, use `POST /api/session/parallel` `{session, n}` (`n` is an integer ≥ 0 or `"default"`; a core refusal is 409 with a reason). Since
  1.19 `queue` in the snapshot holds the mini-hub executors' requests waiting in the queue `[{name, by, pos, wake, task, why}]` (`by` is the mini-hub, `pos` is the number in the common queue, `wake` means finishing work for an executor that
  handed in, not a new one), and a mini-hub's `label`, while its task is with executors, reads "waiting for executors · N working",
  "executor queued (K)" or "executors: N working, K queued" instead of "free". Since 1.20 `budget` in the snapshot is the limit-saving mode (only with mini-hubs): `null` is
  normal, otherwise `{mode, why, until, queued}`: `economy` means new executors only for the owner's tasks and urgent ones at
  once, `owner` means only on the owner's word; `until` is when the window resets, and `queued` is how many are waiting in the queue.
  Since 1.30, alongside it there is `gate`, the budget gate (hub.json `budget.owner_gate`, `true` by default): `false` means the
  owner's words do not wait at the limit: in `economy` the queue is not held (only `--work normal` on a cheap pair), and in
  `owner` they wait for the window to reset.
- Tasks in the snapshot (`delegations`, API 1.30): a handed-in task of a deleted executor goes to the archive after two days,
  `state: "archived"` (the fact `core.archived`); for such an entry `context` and `reply` are only the beginnings of the texts
  (`trimmed`), and the full answer is in the journal and in the task's record. Since 1.31 the snapshot carries only open
  tasks: `working`, `waiting-owner`, `spec` (since 1.36: an executor in spec mode reported, its spec waits for the owner's
  "Accept"; the task's `spec` is `{path, state}`) and `stopped`; `replied` (handed in, hundreds on a long-lived hub) and `archived` are
  not in it, so a client that counts "handed in" no longer gets them from the snapshot: they stay in the core state
  (`core.journal.load_state`, `Tail.reader`) and in the task records (`hub task list`). In that core state a `done` task
  of an executor deleted two days ago keeps its state, but its `context` and `reply` are the beginnings too (`trimmed`;
  the full task is in its record, the full answer in the journal). The first frame of
  `/api/stream` is the same snapshot, so it got lighter too: a session's `info` keeps only `model`, `effort`, `version`,
  `launcher`, `harness`, `backbone`, `launch_generation`, `mcp_failed` and `mcp_auth`; `updates` have no `notes`; `code`
  has no `changes`, and `code.unpublished` is an empty list (the number is `code.unpublished_count`). The full `code` card
  and the last 20 `updates` with their `notes` are in `GET /api/about`.
  `GET /api/about?lite=1` (`about.lite` in `features`) is the same card without the heavy parts: `updates[].notes` is `[]`
  with `notes_count` next to it, `code.changes` is `[]` and `code.unpublished` is `[]` as in the snapshot (the number is
  `code.unpublished_count`), `lite: true`; it carries an `ETag` (`If-None-Match` → 304). The card has the journal's size,
  so on a working hub the tag changes with every event. For hub discovery and signing in; without the parameter the
  answer is the full one, byte for byte as before.
- A request a module holds back (API 1.36, Module API `task.admit`, above): its row in the snapshot's `queue` has the
  module's reason in `why` ("held by the module <name>: <reason>"); nothing else in the row changes, and `pos` counts it
  among the queued.
- The app's code layer (API 1.36): an app hub runs its code from a layer — a release folder without git with
  `MANIFEST.json` (core/appcode.py). The snapshot key `app_code` is present only on such a hub once something happened:
  `{name, count, paths, at, restored?}` — `name` is the layer (`<version>-<flavor>`), `count` the files that differ from
  the release (0 — the release again), `paths` up to 50 of them; `restored {at, why, count, paths, copy?, error?}` — the
  shell's launcher put the release back: `why` is `start` (the core did not come up on the edited layer) or `owner`;
  `copy` is the folder where the edited files were kept. `POST /api/app/restore` asks the shell's launcher to put the
  release back and restart the hub: 202; 409 on a hub that is not an app layer or whose launcher is not known yet.
  Its update (modules/update/layer.py): `update_check.layer: true`, a version without `behind`, and the results
  `offline` (the update server did not answer), `broken` / `bad-archive` (a download broke off / did not pass the
  check — "Update" tries again), `no-source` (a build without a server), `restarting` (installed, the switch waits for
  the full restart); `update_check.ask_restart: true` — installed in the background, the switch waits for the person's
  restart (a hub on a layer installs on its own and asks; the owner's `hub update` restarts by itself and has no
  flag); `version` and `previous` up to 64 characters (`1.9.3-nightly.<time>.<sha>`), and
  `POST /api/update/rollback` takes such a version too. `/api/about` → `code.layer` — the layer the page came up on.
  A build of the person's edits (`hub app apply`, `core/ownsrc.py`): `code.own {release, edits}` — the release it is
  over and how many commits of edits (`code.version` is the build's own `<release>+local.<sha>`), and
  `update_check.own {release, edits}` with `previous` naming that release ("Return the released" takes the edits off).
  A release that needs a newer app (`needs-runtime`) whose app is on the server: `update_check.shell {runtime, size,
  auto, deb?, file?}` — `auto` means "Update" does it (puts the new app in place; with `deb` it downloads the .deb and
  `file` names it for `apt install`); then `shell-ready` (checked, the swap waits for the full restart),
  `shell-downloaded` (Linux) or `bad-shell` (did not pass the check — "Update" tries again).
  Below the distribution server's minimum version: `update_check.min {version, below: true, reason: {language: text},
  stops, since?, ahead?}` — `stops` names what stops working below it (`claude`, `update-format`, `bus`, `stats`),
  `ahead` — the minimum starts on the day `since`; the page draws a band with "Update". `update_check.report {on}` —
  the build sends reports of failed updates to the developers, `on: false` when `update.report` is off.
- The reminder row of the feed (API 1.36): a `remind` row has `md` next to `text` — the same reminder as Markdown, a
  paragraph per question of the card, the question sentence in bold, a long `file://` or Figma link short (the address
  is the link target). `text` is unchanged (one line, the questions joined with " / "); old rows in the journal have
  no `md` — read `text` then. An `AskUserQuestion` option's `preview` (free text) reaches the question card as sent: the
  page shows a sketch as text and one path to a picture through `/api/file` (no new route).
- A turn cut by an account limit (API 1.36): a session's row in the snapshot has `limit_cut`, `{ts, resets}` while the turn
  was cut and the core has not continued it, otherwise `null`. `resets` is when the window that refused the turn resets (a
  time, like `ts`), `null` if the core does not know it (see the slice, above). The core continues the turn itself (after the reset,
  or on another account of the pool), so a client shows «waits for the limit reset» for as long as the row is there, not
  until `resets`; "Continue" is any message to the session. The account and the task the core continues with are not in it.
- Opus's safety filter moved a session to another model (API 1.37): a session's row in the snapshot has `refused`,
  `{state, asked, got, ts, day, again?}` or `null`. Claude Code answers on `got` (Opus 4.8) after the filter stopped two
  answers on `asked` (Opus 5.5); `state` is `waits` — the core raises it again on `asked` once it is free, with the
  stopped turn cut out of the conversation; `back` — done, shown for 24 h; `again` — the filter tripped again within
  24 h of a return: it stays on `got` until something raises it (the owner's reload, a wake), which cuts it too.
  `day` — how many times the filter moved a session of the same account in the last 24 h. A core before has no field.
- A session's helper agents (API 1.38): an entry of a session row's `background` with `kind: "agent"` has `model` when the
  start of the call named one — the Codex subagent's model, or the `model` of Claude Code's Agent call (without it the agent
  runs on its definition's or the session's model, which Claude Code does not say, and the entry has none). The page's row
  shows them as "3 helpers · <model>"; a core before has no field.
- The app's start without a wizard (API 1.36): on an app hub `features` has `setup.app` — its steps run
  by themselves (modules/setup/auto.py) and one band shows them (no hub has a «Setup» tab any more: the band says what
  holds the hub, and "About the hub" has a card for each thing not done). `GET /api/setup` of such a hub always gives the whole
  list, with `app: true`, `host` (`claude` | `codex`), `update` — the layer's download now (`{version, phase
  download|check, done, total, first}` or null), `waits` — `{step: when it starts again}` of a step that failed for the
  network, and `steps_running`; no `team` item, and the steps `github` / `access` answer 400. The `claude` item has
  `manager` (`npm` | `brew` | `anthropic` | `other`) for an older one; npm's or Homebrew's is updated by the step
  `claude_upgrade`, not installed over. The `codex` item has `need`, `own` and `job` (the step `codex`: Codex CLI from
  OpenAI's releases). `POST /api/setup/host {host: claude|codex}` (the app only, before a sign-in) writes the hub's
  host and restarts the hub: 202, 200 `same`, 400. `update.failed` with `why: rolled-back` keeps `by` (`timeout` | `owner`)
  in `updates`.
- The hub's own edits of the core (API 1.33): `update_check.local_edits` in the snapshot is `{count, files: [{path, st,
  zone?}], zones?, base?}` — the files of the hub's code folder that differ from the tree of the release inside the code (`st`: `M` changed,
  `A` added, `D` deleted, `?` a new file outside git). `base` is the full hash of the commit they were counted from: the
  newest commit of the branch the updater follows that is inside the hub's code (the merge-base, the same rule as the update
  self-check; without that branch — the highest release tag inside the code), so a release merged by hand or already inside
  counts as the release, not as edits; the install mark when that commit is not ahead of it. `base` is absent on a core of
  an earlier 1.33 (v1.7.1 reported 1.33 with `local_edits` but without `base`, `gist.lead` and the queue row's `about`; v1.8.0
  added them under the same number, and no `features` entry names them — test the field, not the version). The tree is compared, not the history: the updater's merge of a release and an own commit that was taken
  back are not edits. An edit of the core is a path the release has that is changed or deleted, or a new file in a folder the release has (a new `core/x.py`, `tests/test_x.py`, a new file of a module the release
  has); a whole new `modules/<name>/` with its `module.json` (and its `bin/hub-<name>`) is the hub's own module: it is not
  counted and is named in `local_edits.modules` (up to 20 names; the page shows a calm line, not the band); what
  `.gitignore` hides is not counted. The same rules (`core/codeedits.py`) decide whether the update self-check runs the tests. `files` is sorted and has up to 50 entries, `count` is the whole number; `0` — none; the
  key is absent on a core of an earlier version, on a hub that does not update and in the app. API 1.36: what an edit touches, by its path (`core/codeedits.py` `ZONES`): `zone` of a file is one of `guards` (what
  sessions may run, who may answer for the owner), `access` (who reaches the page), `data_out` (data that leaves the hub,
  sign-ins), `update` (the update's own check), `journal` (the journal and the fold), `prompts` (rules every session
  reads), `tests` (the release's tests); absent for the plain core. `zones` is `{zone: files}` over all the edits (whole
  counts, only zones with edits; absent when none); an older core sends neither. The band names each zone in a line of its own. The page draws a red band
  above every tab of "Settings" (the files under a spoiler) with a "Fix" button: `POST /api/update/fix {}` hands Hub
  Developer the task of sorting the edits out (what the release already has — drop, what everyone needs — a PR, what is the
  hub's own — a module or a plugin, the rest — back to the release, a backup branch before any reset): `202 {ok, text}`, `409`
  while there are no edits. Why: a hub with edits of its own runs the core tests on every update. API 1.36: next to
  "Fix", "Take off" — `POST /api/update/local-edits {to: "off"}` — has the update process take the edits off by
  itself, under the machine lock (`modules/update/takeoff.py`): the whole code folder, uncommitted and new files too, goes
  into a backup branch `backup/local-edits-<date>`, the files of `local_edits.files` go back to the release with a new
  commit, the hub's own modules stay, the hub restarts as after an update, the result comes to the hub's feed
  (`owner.notify`). `{to: "on"}` merges the edits of the last such branch back on top of the code (a conflict leaves the
  code as it is and hands the merge to Hub Developer). `202 {ok, text}`; `409` — nothing to take off (`count` 0) or to
  put back (no `backup`), or a press already on its way; `400` — another `to`; an older page server answers `404`.
  `local_edits.backup` is `{name, count, at}` — the last take-off not put back yet (`count` files, `at` Unix seconds),
  absent otherwise; the command is `hub update local-edits off|on`.
  What the update found against the release (API 1.36, `modules/update/shadow.py`; neither holds an update):
  `local_edits.shadow` is `{failed: [test names], count, version?}` — the release's versions of the test files the hub
  edits went red on the hub's code (a run after the install, outside the self-check, without the browser), at most 10
  names, `count` whole; `local_edits.lost` is `{items: [{id, path}], count}` — hunks of a release the merge may have lost
  in files of the zones the hub edits, open until Hub Developer answers `hub update lost ok|restored <id>`, at most 10.
  Both are absent when there is nothing to show; an older core sends neither.
- MCP servers waiting for a login (API 1.31): `mcp_waits` in the snapshot is `[{server, since, sessions, host?}]`, the
  oldest first. `since` is when the server started waiting (its first report; a session restart does not reset it);
  `sessions` are the sessions (not removed, sleeping ones too) that wait for it; `host` is where the server is, when its
  address is known — two servers under one name (`.mcp.json` of two projects) are two entries, and a login into one does
  not clear the other. A server leaves the list after a login (`hub mcp login`), once a session reports it connected at
  the same address, or once no session names it. Empty list: nothing waits; a hub of an earlier version has no key. A
  session's `info.mcp_auth` is still there, but it is dropped at every restart, so it does not say for how long. The core
  command `hub mcp reconnect <server> [--session <name>]` reconnects a server right now (the fact
  `mcp.reconnect_requested`; one server in one session not more often than once per 30 s). An entry also has `fp` — the
  fingerprint of the server's address ("" while it is not known) — and `confirm: true` for a server on the hub.json list
  `mcp_login_confirm` (a sign-in to it needs the owner's confirmation).
- A sign-in to a waiting MCP server from the page (API 1.31, the page's "MCP waits for a login" band): `POST /api/mcp/login
  {server, fp?, confirmed?}` — the core signs it in through a live session where it waits (Claude Code's control
  requests; no such session — the way of `hub mcp login`). Answers: 200 `{attempt, url}` — open `url` in a new tab;
  200 `{attempt, done: true}` — it was signed in already; 202 `{attempt, pending: true}` — no link yet, ask
  `POST /api/mcp/link {attempt}` (the same answers); 409 `{error, why?}` — refused (it does not wait, a server from
  `mcp_login_confirm` without `confirmed: true`, a second press within 5 s) or the sign-in did not work. When the browser
  runs on another device, the address from its bar after the sign-in goes to `POST /api/mcp/callback {attempt, url}`
  (202). The link and that address are never in the snapshot or the journal; an attempt lives about 5 minutes. The
  snapshot's `mcp_logins` is `[{id, server, fp, host?, session, state, ts, at, error?, pty?}]`, the latest first: `state` is
  `asked` (the link is being made), `open` (the link is out; `error` — the address given was not accepted), `sent` (the
  address is being checked), `done` or `failed` (`error` — why); a finished attempt stays at least 10 minutes, and one
  still `asked`, `open` or `sent` after about 5 minutes has run out (the page says so).
  `hub mcp login <session> <server>` asks the core for the same sign-in: the fact `mcp.login_requested {server, fp?,
  session?, confirmed?}` is taken from the owner (the page, his terminal) and from a session (`session` — the one to go
  through first); a plugin or module process is refused, and a server from `mcp_login_confirm` needs the owner's
  `confirmed: true`. The address after the sign-in: `mcp.callback_given {attempt}`, from the same callers.
  The cross on the band (API 1.36): `POST /api/mcp/dismiss {server, fp?}` (200, or 409 `{error, why}`) — the fact
  `mcp.wait_dismissed {server, fp?}`, from the owner only (the page; there is no `hub` command for it; a session, a plugin
  or a module process is refused). The core marks the wait (`dismissed` in its entry) and `mcp_waits` no longer lists it; the server still
  waits for a login. The mark lives as long as the wait: a restart or another report of it keeps it closed, and a login, a
  session that has the server connected, or no session naming it any more starts the next wait anew — it is listed again.
- The page's password (API 1.36, `core/pagepass.py`, `page.password` in `features`; set per hub in "Settings → General →
  Hubs on this computer" or by `hub password set`, off by default). A request passes in two steps: first who came —
  this machine (127.0.0.1, Host localhost, no proxy) or the owner as a provider names him (tailnet `whois`, Cloudflare
  Access; anyone else is 403 before any form); then, when the hub has a password, the cookie of its sign-in form, from
  anywhere. Without it `/api/*` and "Files" answer **401 `{error, code: "signin"}`** and `GET /` is the sign-in form
  (a plain HTML form, no hub data). The form posts `password=<…>` to `POST /signin` (urlencoded, no `X-Hub`): 303 to
  `/` with the cookie, 401 wrong (the form again), 429 after five wrong tries in a row (a minute, doubling up to an
  hour; the counter is the hub's, whoever checks). The cookie is the hub's (`hub_main` for the main hub, `hub_<name>`
  for an instance; HttpOnly, SameSite=Lax, `Secure` through Cloudflare), 30 days, renewed by a page load once a day;
  a new password signs every device out. `POST /api/signout` (X-Hub) drops this browser's cookie. `GET /api/signin` —
  `{signin}` (signed in: 200; a client whose stream keeps failing asks it, 401 `signin` means: show `/`). A client that
  calls the API itself (an app) shows `/` in its web view on a 401 `signin` and sends the cookie it got there with its
  requests and its stream. `GET /api/about` has `signin`: `open | password | key | broken`; without the sign-in, from
  this machine only, it answers just `{core_started, web_started, code: {running, layer?}, signin}` (the update's
  watchdog, a neighbouring hub's page). `key` — the earlier sign-in by a link with a key (`core/pagekey.py`) was on and
  there is no password yet: as before, from this machine only with its cookie, given by `GET /?key=<key>`; from a
  provider without it. `broken` — a password file that is not one: closed for everyone. The hub's own `hub secret fill`
  reaches `POST /api/secrets/fill` with the header `X-Hub-Local` (the token in `<state>/page-local`, 0600, new at every
  start of the page) — that path only. A "Files" report (`/render/<root>/…`) is redirected to `/render/~<mark>/<root>/…`:
  its sandboxed page loads its own files without the cookie, by the mark (the report's folder and below, the day it was
  opened and the next; signed by the password's secret: changes with it). Cookies named `hub_<…>` are the hubs': the
  page drops them (and Cloudflare Access's `CF_<…>`) from what it passes to a module behind it (`/mod/…`,
  `/api/mod/…`) and from a module's `Set-Cookie` — a module names its own cookies otherwise. `GET/POST /api/page-key`
  and the hint about other accounts of the machine are gone.
- The hubs of this machine (API 1.36, "Settings → General → Hubs on this computer", `modules/web/instancesweb.py`):
  `GET /api/instances` — `{hubs: [{name, title, main, me, command, port, running, gone, port_busy, engine, claude,
  signin, password_ok}], add, engines, engine, codex_found, job, unprotected}`: `name` is `""` for the main hub, `me` — the hub of this page, `claude` —
  `own|shared` sign-in, `add` — why a hub cannot be added here (or null), `engines` — what the form offers (`codex`
  wherever OpenAI builds Codex CLI for the machine), `engine` — this hub's, the form's default, `codex_found` — the
  machine has a codex a new hub takes (false: the new hub downloads its own copy), `job` — the page's last
  `{op: new|remove, name, state: running|done|failed|lost, lines?}`, `signin` — the hub's page password state
  (`open | password | key | broken`), `password_ok` — its running code takes a password, `unprotected` — the names of
  the hubs of this OS account without one (`core/pagepass.unprotected`; a hub without it opens the others). Nothing of
  another hub's data. `POST /api/instances {op}` — the owner's step: `new {name, engine?, claude?}` (the refusals of
  `hub instance new` at once, 409; then 202 and the install runs apart from the page's server, `core/instancejob.py`),
  `remove {name, confirm | password}` (an instance only, `confirm` repeats the name — for a hub with a password,
  `password` is its password; its data stays), `clear` (forget a finished job); another job running — 409.
  `password {name, new, current?}` — set or change a hub's page password (`current` whenever it has one; this hub's
  answer signs this tab in with Set-Cookie), `password_off {name, current}`; 400 too short or long, 403 a wrong
  `current`, 429 too many tries, 409 a hub on an earlier version or closed by its old key. An instance's page and its app manifest name it by its instance name when `title` is not set (`hub_name`,
  `hub_title`, the manifest's `name`).
- Voice to text (API 1.30, transcription is configured by the `voice` section in `hub.json`, otherwise 404): `POST /api/voice`
  `{data, mime?, session?}`, with the recording as base64 and `session` being who the owner was speaking to (optional; it is
  used to put `context` into the answer on failure). 200 `{text}`; 422 means there is no speech (a repeat will hear the
  same: do not repeat); 400 means the recording could not be parsed or is empty; 413 means it is above the limit (API 1.32: 20 MB of audio; `error` is a phrase with the limit and what to do, in the page language; a hub from before this answered 400 with "empty or too long"); a transcription failure
  is 502 `{error, id, file, reason, context?, partial?}`: `error` is a phrase without the reason or a path on disk, `reason`
  is the cause in the hub's short words (recording format, ffmpeg, engine: did not start, did not finish in time, crashed,
  empty output; a hub from before this change has no such field: do not rely on it), `file` is the address
  `GET /api/voice/failed?id=<id>`, `context` is where the recording was going and what the conversation there is about, and
  `partial` is what the engine managed to transcribe. A failed recording is kept by the hub only for the owner (`Range` is
  supported, up to 7 days and no more than 200 MB in total: an old one can be 404); the same audio in a repeated request gets
  the same `id`, with one copy.
- A recording as raw sound (API 1.36, `voice.raw` in `features`, present while voice is on; without it use the base64
  `POST /api/voice` above): `POST /api/voice/raw?mime=<type>&session=<name>`, the body is the sound itself (up to 20 MB,
  `Content-Length` is required), the answer is the one of `/api/voice`. Large or unsteady uploads go in ONE session: the
  first chunk with `done=0` answers `{id, size}`; the next ones add `id=<id>&offset=<size the hub has>&done=0`; the last
  one is sent without `done=0` and answers the text. `mime` and `session` are kept with the session from the chunk that
  has them (the first; a later one may change them). A chunk with `id` repeated after a lost answer or overtaken by its
  own retry adds nothing (the hub counts the size after the body, under a lock); an `offset` beyond the size is 409
  `{error, size}`: continue from `size`. A repeated first chunk (no `id` yet) opens another session: continue with the
  `id` you got, the other one expires. A `done=0` chunk to a session that has ended is 404. A last chunk repeated while
  it is transcribing, or within 5 minutes after, gets the same answer. An unknown or expired session (an hour without a
  chunk; at most 16 are open, the oldest go first) is 404; a wrong `offset` (not ASCII digits, or none with `id`) is
  400; a chunk that would take the recording past 20 MB is 413 and ends the session; a refusal before the body is read
  closes the connection. Session files are readable by the owner only (0600).
- Plugin catalogs (API 1.30, the marketplaces spec): `GET /api/marketplaces` → `{catalogs: [{name, source, official, commit, updated, entries, skipped, problem, error, description}], entries: [{name, catalog, type: "plugin" | "module", pinned, version?, description?, category?, author?, tags?, hubs?, rights?, revoked?, icon, repo?, installed?, newer?, wider?}], official}`: `catalogs` are the connected catalogs (`problem` is why it cannot be read, `skipped` is how many entries the
  check discarded), and `entries` are the entries of the readable ones; `installed` is the key of the installed plugin or
  module, `newer: true` means the catalog has a newer version, `wider` is what the new version asks for beyond what was
  approved, in the catalog's words (`null` means the catalog did not name the rights, and the install will ask); `rights` is
  the catalog's words, not a permission. `GET /api/marketplaces/icon?catalog=<name>&entry=<entry>` is the entry's icon from the
  catalog clone (the page does not go to foreign servers; up to 256 KB). To install: `POST /api/plugins/install` `{entry: "<entry>@<catalog>", preview?: true, accept?: true, at?}`: exactly the entry at the catalog's commit. `preview: true`
  installs nothing: 200 `{ok, at, preview}` gives the rights the plugin will ask for according to its manifest at this commit
  (`at` is the catalog's commit; send the same one with the install: if the catalog was updated in the meantime, it is
  refused with 400, since the owner saw something else). Without `accept: true`, an install or update with rights wider than
  approved is 409 `{error, wider: [rights in words]}` and nothing is installed; with `accept: true` it is installed, and the
  consent is recorded as a `plugins.consented` event (and a message in Telegram). A core module from a catalog is 400: it is
  installed by the `hub marketplace install` command. `POST /api/plugins/update {name, at?}` and `/api/plugins/remove {name}` for something installed from a catalog go through the catalog's installer: to the catalog's version and into new
  storage; `reviewed` is 400 (code review exists only on the earlier path). The same applies to an entry installed before
  catalogs (without `market`) whose source is pinned in a connected catalog. `GET /api/plugins/updates` ("newer exists" on the
  earlier path) does not list such entries: their version is in `entries[].newer`. `GET /api/plugins/search?q=` is the plugins sent to
  review by link (`link: true`) until installed (since 1.36: the hub's earlier list and GitHub accounts are gone, the
  `trusted` and `legacy` entries no longer come); `install` of a repo outside connected catalogs answers 400 without
  `reviewed`. Deprecated (`DEPRECATED`, until 2.0): `hub market`. In the snapshot each plugin (`plugins[]`) has
  `page_access` (`"full"` means it has page code: full access to the page, declared or not; otherwise `null`) and `market`
  (the name of the catalog it was installed from; no field means not from a catalog).
- The direction board (API 1.30): `GET /api/backlog?slug=<direction>` also returns `facts`, the core's facts about cards
  (`core/board.py`): `cards` `{TASK-N: {kind, …}}`: `working` (an executor is working on it: `who`, `gist`, `waiting`,
  `stuck`, `handed` out of `total`), `unsorted` (all linked ones handed in, and the lead or owner did not touch the status
  and notes after the handoff: `since`, `who`), `unknown` (the file was changed after the handoff and there is no fact of an
  edit: `since`), `idle` (In Progress, no live linked ones: `last` `{ts, by, owner?}` or `{updated}` from the file);
  `unlinked` is the direction's executors with no link to a card within a day `[{name, about, state, since}]`; `counts`
  `{working, unsorted, unlinked}`; `lag` (1.31) — the board lags behind the work `{since, age, delegations, owner}`: after
  the last card edit (a card file or an edit fact) the direction's lead got `owner` words of the owner and gave
  `delegations` tasks to its executors without a card, the oldest `age` seconds ago (more than 5 min); no lag — no key. If the journal
  reader is busy, there are no `facts`. In the snapshot,
  `direction_tasks[<direction>]` has the same `working`, `unsorted`, `unlinked`. The core does not set Done: the facts only
  show where the board diverges from the work.
- `owner.lang` in the snapshot is the owner's language as a code (`ru`, `en`; since 1.25 also `uk`, `ja`, `zh-CN`, `ko`, `hi`,
  `ar`, by the script of the owner's messages), if the hub has already determined it; Latin script gives `en`, as before.
- Captions in the snapshot (a session's `label`, etc.) are in the hub's language; for clients and plugins, use the `state` and
  `status` codes.
- Codes instead of text (API 1.25): the texts the core writes (the "from whom" labels, refusal reasons, service feed rows, the
  `hub wait` checks) are translated and change; read the codes. A service feed row (`kind: "sys"`) has `code`: `created`,
  `removed`, `resumed`, `compacting`, `handoff` or `job` (a row about a schedule job, with the id in `job`); a job in
  `/api/schedule` has `wait`, what the `hub wait` check is waiting for; in the journal `core.rejected` has `code`
  (`core_failure` is a core code failure, `duplicate` is a repeat of `client_id` or `dedup_key`); a message in the core state
  has `from` `{kind, name}` (`owner`, `session`, `hub`, `core`, `schedule`, `module`, `plugin`; `name` for a session, module
  and plugin). Since 1.26 the same `from` is on the `in` feed row (an incoming message and its mirror at the owner's word):
  fold core or module service messages by it, not by the text; for old records go by the label, and with no label it is
  `core` for a core message and, for an incoming one, `owner` or `null` (unknown); a hub of an earlier version has no field.
  A module that edits the addendum (`prompt.parts`) should likewise not look for strings in it: the part texts are
  translated. Old records have no codes: there it is text.
- The core's words to a session (API 1.36: "a hub restart cut off your turn", a limit continuation, the executor's
  unmerged branch, the tail of a relayed reply…) go to the session in English; an `in` feed row with such words has
  `label` — the same text in the hub's language, as the page shows it — while `text` stays what the session read. A
  client shows `label` when there is one; an English hub, an earlier record or a hub of an earlier version has no
  `label` there. The same for a refusal: `core.rejected` in the journal has `l10n` next to `reason` when the reason is
  made of the core's phrases; the page's `/api` refusals (`error`, the receipt of `/api/send`), Telegram and `hub` in
  the owner's terminal already give the reason in the hub's language, a module reads `reason` as the session does.
- The label of a message the session reads (API 1.36) is English: `[hub:mN from user]`, `from core`, `from hub`,
  `from schedule`, `from module X`, `from plugin X`, `from <session>` — a session named `user`, `core`, `hub` or
  `schedule` (other than the hub session) is `from session <name>`; earlier records have the Russian labels of then.
  Read who sent it from the `from` field, not from the label. The `sender` of an `in` feed row the core made is a code:
  `:core`, `:schedule`, `:owner` (a session name has no `:`), `mod:<name>`, `ext:<name>`, `hub` or a session name (the
  parse of an archive made by an earlier core is given the same way); a schedule job's `by` is `:owner`, `:core`, `hub`
  or a session name, and a `hub wait` check job has `wait_gist` — its text up to "Check now".
- The core's note about the owner's action on a card (API 1.36, `feed.on_behalf` in `features`): when the owner answers
  a question card, closes it or withdraws it, the core writes the session a message in his name ("The owner's answer to
  question qN: …", "The owner closed your question…", "Your question to the owner qN… is withdrawn"). The core marks such
  a message when it writes it; its `in` feed row has `sender: ":core"`, `from: {kind: "core"}` and `on_behalf: "owner"`
  (plus `answer_to: "qN"` for an answer or a closed card; a withdrawn card's note has none). Draw it as a service row of
  the core, never as the owner's own words — his answer by a card's button is the `answer` row of the same `seq`, a
  typed answer is his own `in` row; a closed or withdrawn card has no row of his, the note is its only trace. A hub without
  `feed.on_behalf` gives such a note its old row: `sender: ":owner"`, `from: {kind: "owner"}`, no `on_behalf`; a hub with
  it gives the old row only where the core has nothing but the message stored before the update (the past of a feed from
  journal archives). The owner's own words have no `sender`, so draw any `in` row with a `sender` as not his. The past
  of a feed from journal archives parsed before the update keeps the rows it was parsed with: there such a note can
  still come as the owner's own words (no `sender`); the core does not parse old archives again for this.
- A Telegram attachment that the bot did not download (larger than 20 MB, a Telegram error, the network) is not appended to
  the owner's `text`: the `in` feed row and the `message.received` event have `attach_failed` `[{name, size?, reason}]`
  (`reason` is the cause in the hub's language; the bot writes the same to the owner in Telegram). Earlier records have no
  such field.
- An external agent's letter (1.31): a core module that relays a message from an external agent (Codex, your own script) to
  a session puts the agent's name into the data of its `message.received` as the `agent` field; on the `in` feed row it is
  `agent` (one line, 1-40 characters, without control or invisible characters; otherwise there is no field), next to
  `sender` `mod:<module>`. The byline "agent X · module Y" comes from these fields; do not parse a label in the message
  text. The field is taken only from a module's fact (channel `plugin`, sender `mod:<name>`, not the owner): the page,
  Telegram, the owner's words from a module handle, sessions and plugin processes (through `hub` commands) cannot pass as
  an agent. Earlier records and hubs have no such field.
- The words of another hub (1.32): an `in` row of a core message that carries the words of another hub (the liaison passed
  them, or the core handed them straight to the session whose request they answer) has `hubbus` `{contact, body, agent?,
  thread?, direct?}` — the contact's local name, the words alone out of the core's frame (`text` keeps the frame as the
  session got it: the warning, the reply command, the `<<<bus-…` markers), and who wrote there (`agent`: one line, 1-40
  characters, without control or invisible characters; otherwise none). Draw the words from these fields; do not take
  them out of the frame text. The name the owner calls the hub is in the snapshot `bus.contacts` (`alias`, else `label`).
  A session that asked the liaison to pass words to a hub (`hub send liaison --to <contact>`) has a row of kind `bus` in
  its own timeline: `{kind: "bus", dir: "out", contact, text, id}` — the first words of the thread, which the answer
  (`hubbus.contact` the same) continues; a page that does not know the kind shows a service row with `text`.
- Session list groups (API 1.16): `layout` in the snapshot is `null` until the owner has touched the layout (the standard
  groups by kind: `agents` are specialists, `directions` are directions, `workers` are the rest); otherwise `{rev, groups: [{id, title}], place: {session: group id}, order: {group id: [sessions]}}`, and after "Reset" it is `{rev}` without groups.
  A session without `place` is in the group by kind; an executor is under whoever brought it up. To change it, use `POST /api/layout` `{layout, base}` (the whole layout and the `rev` it was computed from; the response is `{seq}`, the new
  `rev`; someone else's `base` is 409 with a reason). `own_icon` on a session is the owner's own icon (the same as in
  `icon`), `POST /api/session/icon` `{session, icon}` (empty removes it).

## Changing the hub itself

**For yourself.** If you cannot do without code, Hub Developer edits and commits to your local `main` and does not push. The
shared hub's updates are merged on top; on a conflict or red tests, the update is rolled back, and Hub Developer gets a task
and works it out.

**For everyone.** Hub Developer makes a branch from the shared `main` with a single change, without anything personal
(names, projects, keys), with a test, and opens a PR to the shared repository. The shared hub's maintainer reviews and
accepts it; the change reaches everyone with an update.

## Updates

The `update` part runs `git fetch origin main` once an hour (`update.every`; a hub from the one-line install asks its
delivery server every 6 hours instead); if there is something new, it merges it, runs
the tests, and if they are green the hub restarts when sessions are free, and the page reloads itself. To run it now, use
`hub update`. Turn it off with `"update": {"enabled": false}`. Without an `origin` remote (the shared hub itself) it does
nothing. A hub from git has one channel, `main` (nightly): `update.channel` is not read (an earlier `stable` means `main`),
`update.branch` names another branch to follow.
