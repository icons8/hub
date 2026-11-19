"""The hub's plugin API version (owner decision 2026-10-03: core versions are needed so that there are warnings that a
plugin will no longer be supported). This is not the version of the hub's code but the version of the contract with
plugins — `hub-plugin.json`, `hub.*` on the page, facts into the mailbox (EXTENDING.md). The minor number grows when
the contract is extended (old plugins keep working); the major one when something in it is removed or changed
(plugins on an old major are not loaded).

A plugin states in its manifest which version it works with: `"hub": "^1.0"` — 1.0 and newer within 1.x; `">=1.2"` —
1.2 and newer within 1.x; `"1"` — any 1.x. Without the field it loads, with a warning.

DEPRECATED — what in the contract is obsolete: the page and `hub market` warn that it will stop working.
"""
import json
import os
import re
import subprocess

PLUGIN_API = "1.37"  # 1.37 (17.11, not released): the plugin record's `css` — a list of up to 8 stylesheets of the plugin (`.css` inside its folder, hub-plugin.json or hub.json) that the page puts into its <head> itself, so a plugin's tokens are there from the first frame; the snapshot's `extensions` entry has `css` when the plugin lists some, and `hub.addStyle` of a listed file does nothing (EXTENDING.md); GET /api/session/kit — a session's set of skills and MCP servers and what it used (core/uses.py; 18.11: `volume` — the period's conversations and turns, `convs` of a use, `tokens` of the set's list); a session's row has `refused` {state waits|back|again, asked, got, ts, day, again?} or null - Opus's safety filter moved it to another model (EXTENDING.md); a theme's own settings - hub.addOptions({ themes: [<theme file of the plugin's manifest>], hint?, options }) is a group shown in "Settings -> Look" under the row of that theme and only while it is chosen (id <plugin>:<file>, as /api/themes names it), hub.capabilities.themeOptions === 1; without themes a group stays on "Look" as before; GET /api/file?path=…&w=<px> of a picture with an alpha channel is a png (image/png, the name ends in .png; its weak ETag has -png before the -w<px> tail), not a jpeg painted white; every /api/file answer has X-Hub-Thumb: the copy's width, 0 for the original, on a 304 too; an `in` feed row of a member of the Telegram group (from.kind member) has `member` {name} - a person who only asks, not the owner: draw it as another person's, not as his bubble; 1.36 (06.11, not released): a recording as raw sound in one upload session - POST /api/voice/raw (`voice.raw` in features, EXTENDING.md); the owner's words to a session that is gone or never was — POST /api/send to a session-shaped name missing from the snapshot is 202, not 404 (the core passes them to the hub, or to whoever raised a removed executor), and that `in` feed row has `addressed`, the name he wrote to; `ref` in POST /api/send — the id of a client reference or note the owner answered with words, into the message.received data as is (a string up to 128 characters, null is no field, anything else 400; the core does not read it); "Continue" on a session paused by a limit - POST /api/session/resume {session}: the core continues the cut turn now (202) or answers 409 {error, code: not_paused} | {error, code: not_yet, until}; `session.resume` in features; `about.lite` in features: GET /api/about?lite=1 is the same card without the notes of past updates (`notes` empty, `notes_count`), `code.changes` and `code.unpublished` (empty), `lite: true`, with an ETag (If-None-Match -> 304); without the parameter the answer is the full one as before; `stream.delta2` in features: GET /api/stream?delta=2 — a patch carries the changed items of a big keyed section under `_delta` ({by?, set, del, order?}) and its frame number `_n` (1, 2, … per connection; a misfit — reconnect for a snapshot); without the parameter (and with delta=1) patches are whole sections as before; `stream.feed` in features: GET /api/stream?feed=<plugin>[,…][&feed_since=<N>] also carries the new posts of those plugin channels as `event: feed` {plugin, posts} (the rows of /api/feed), after the stream's first snapshot or after post number N; without the parameter the stream is as before; the `remind` row of the feed has `md` next to `text` — the same reminder as Markdown, a paragraph per question of the card (absent in old rows: read `text`); the snapshot's queue_paused.by can be mod:<name> — the pause of the executor queue that a core module with the queue.pause right set (Module API 2.10; the owner's is still null, a session's or the hub's is its name; the owner's `hub task resume` lifts it); a hub from the one-line install running a build of the person's edits (`hub app apply`): code.own {release, edits} and update_check.own {release, edits}; "Files" in the side panel can leave the window header to the page — `/open?…&embed=1&chrome=hub`: the window tells the hub page (postMessage `files:state`, only to the page's exact origin) the file, its path and its actions, runs an action the page names by id (`files:do`, only from that origin, navigation and reading only) and asks the page to close the panel on Esc (`files:close`); without `chrome=hub` the window is as before; the label a session reads is English ([hub:mN from user]); a session named user/core/hub/schedule is from session <name>; the sender of an in row the core made and a job's by are codes (:core, :schedule, :owner, mod:/ext:), a wait check job has wait_gist; an `in` feed row with the core's words to a session has label — the same text in the hub's language (text stays the English the session read; core/coretext.py), and core.rejected has l10n next to reason — /api refusals, Telegram and the owner's hub give the reason in the hub's language; update_check.local_edits.zones {zone: files} and files[].zone (core/codeedits.ZONES: guards, access, data_out, update, journal, prompts, tests) — what the hub's own edits of the core touch; POST /api/update/local-edits {to: off|on} — "Take off" / "Put back" of those edits by the update process (202, 409 nothing to do or already on its way, 400) and update_check.local_edits.backup {name, count, at} — the last take-off not put back yet; update_check.local_edits.shadow {failed, count, version?} — the release's tests the hub edits red on its code, and local_edits.lost {items: [{id, path}], count} — release hunks a merge may have lost (modules/update/shadow.py); GET /api/plugins/search — only plugins under review by link (link: true): the hub's earlier list (trusted.json) and GitHub accounts as marketplaces (markets) are gone, no trusted or legacy entries; POST /api/mcp/dismiss {server, fp?} — the owner closes a row of the "MCP waits for a login" band (the fact mcp.wait_dismissed, the owner only; 200, 409 {error, why}); the snapshot's mcp_waits leaves a closed wait out while it lasts — the server still waits for a login; the snapshot key update_check.layer and the results offline|broken|bad-archive|no-source|restarting, update_check.ask_restart (true: a background install waits for the person's restart; update_check.stands is gone, 09.11), version/previous up to 64, /api/about code.layer — the app's update on a code layer (modules/update/layer.py); the snapshot key update_check.shell {runtime, size, auto, deb?, file?} and the results shell-ready|shell-downloaded|bad-shell — the app's own update to a newer environment (tools/package/shellswap.py); app_code {name, count, paths, at, restored?: {at, why: start|owner, count, paths, copy?, error?}} — the app's code layer edited and the release put back (core/appwatch.py; only on such hubs), POST /api/app/restore — "Return the released" by the shell's launcher (202; 409 not an app layer or the launcher not known yet); update_check.min {version, below, reason: {language: text}, stops, since?, ahead?} — the app below the distribution server's minimum version, update_check.report {on} — reports of failed updates go to the developers (modules/update/report.py); the sign-in by a link with a key (core/pagekey.py, a setting, off by default): with it on, a request from this machine without the hub's cookie gets 403 on /api/* and "Files" (and the page "by a link with a key" on /), GET /api/page-key {on, broken, link, remote, telegram, others, remind}, POST /api/page-key {op: prepare|send|on|off|new|known} (on and new sign the tab in with Set-Cookie); the hubs of this machine (Settings -> Main -> Hubs on this computer): GET /api/instances {hubs, add, engines, engine, codex_found, job}, POST /api/instances {op: new|remove|clear} — the install runs apart from the page's server; an instance's page without a title is named by its instance name (hub_name, hub_title, the manifest's name); a queue row of a request a module holds back (Module API 2.10, the task.admit point) says so in why: held by the module <name>: <reason>, and `hub task queue` shows the same line; 1.35 (05.11, not released): slot session.header.start — the open session's header before the name, after the back-to-the-list button where it is shown (ctx.session), earlier than the title in the markup and the Tab order; 1.34 (05.11, not released): the "Loading the feed…" stub of a session feed is a `p.hint.feed-loading` — a plugin or theme finds it by the class, not by the text, which differs by language; POST /api/accounts/probe {account?} — a fresh limits reading of a pool account without a switch (one short request with its token; on an account that is not the active one it opens the 5-hour window; without account — every account with its own token): 200 {results: [{name, ok, error?}]}, 404 not in the pool, 409 {error} the sign-in module refused or cannot measure (while it leads session login it measures its own pool), a reading is already running or there is nothing to measure; the new limits come as usual in the snapshot's account.seen, and an account of the "Accounts" pool the reading can refresh has probe: true: Codex (experimental) (a hub without Codex sessions answers without the following; the commands, /api/usage and /api/setup parts — a hub with "host": "codex", the rest — any hub with Codex sessions, HUB_CODEX=1 too): commands in the snapshot of a hub entirely on Codex has codex: true, models as codex/<id> plus "default", titles — {codex/<id>: the model's name} and model_efforts — {codex/<id> | "default": [the efforts that model knows]} (a model missing there takes every level of efforts; "default" — the levels of the hub's model); until Codex has listed its models, models is the hub's model from the settings; /api/usage has unit — the main unit of the sums (usd | tokens) and, on a hub with both Claude dollars and Codex tokens in the shown days, other — the other unit's part of the same form ({unit, days, windows, status, top}; before, the tokens were hidden there); GET /api/setup of a hub entirely on Codex has the items codex ({state, version, path}) and codex_login ({state, via?: own | working, account?, job}) instead of claude and login, the hub's Codex sign-in from the page is POST /api/codex/login; a timeline row of a host recap (session.recap) may be by "codex" too; a permission card of a Codex session asking for wider rights has tool "Permissions" ({permissions, cwd}); /api/subagent of a Codex subagent's step answers the same form with agent {type: "codex", thread, …} (its steps from the session's folder, not a transcript); the Codex account pool: the snapshot's codex has pool {on, rows: [{name, hub?, email, plan, signed, active, lost, busy (until), limits (windows)}], exhausted (until)} and page.account (whose sign-in the code is for), POST /api/codex/login takes {account} — a row's sign-in again (404 not in the pool, 409 its folder is not ready), POST /api/codex/add — a new account and its sign-in: 202 {ok, url, code, account}, 503 the pool's module did not give a folder; 1.33 (03.11, released in v1.7.1 with local_edits and POST /api/update/fix; gist lead / queue about / local_edits.base were added under the same number in v1.8.0, and no `features` entry names them — a hub on v1.7.1 reports 1.33 without them, so a plugin that needs them tests the field itself, not the version): gist on a session in the snapshot is the start of the latest result for the owner when the session has no fresh essence of its own ({text, ts, lead: true}: a short result whole, a cut one by its first sentence, ts — the time of the result); null now means there was no result to the owner or its latest output went to another session — not "show out_head"; a queue row in the snapshot has about — the task (dN) a queued wake reworks, "" for a wake without one (wake is true for both); the snapshot update_check has local_edits — the hub's own edits of the core against the release inside the code ({count, files: [{path, st}], base?}, base — the full hash of the shared commit inside the code that the files were counted from; st: M | A | D | ? — changed, added, deleted, a new file outside git; up to 50 files, count is whole; 0 — none; absent — an older core or a hub that does not update; a whole new modules/<name>/ with its module.json (and its bin/hub-<name>) is the hub's own module — it is named in local_edits.modules (up to 20 names), not counted; a new file in a folder the release has is an edit; plugins and hub-local live outside the folder; what .gitignore hides is not an edit), and POST /api/update/fix {} — the "Fix" of the red band about them: a task for Hub Developer to sort the edits out (202 {ok, text}, 409 while there are none); 1.32 (30.10, released in v1.7): a plugin's own media from its folder — /ext/<name>/… and /mod/<name>/web/… serve jpg, jpeg, webp, gif, mp4, webm, mp3, ogg, wav, wasm, glb with their types (application/wasm, model/gltf-binary), video and sound by Range (206) with a strong ETag and If-Range, file names percent-decoded, nosniff, never a page (it was 404); an `in` row of a timeline with the words of another hub has hubbus ({contact, body, agent?, thread?, direct?}) — whose hub, the words alone out of the core's frame (text keeps the frame as the session got it), who wrote there; a session that asked the liaison to pass words to a hub has a row of kind bus ({dir: "out", contact, text}) in its own timeline (a page that does not know the kind draws a service row); «Setup» of a new hub, when the snapshot's features have setup — GET /api/setup ({ready, blocking, local, items: [{id: claude | login | chrome | team | doctor, state, job, …}], auto}; ?full=0 — only claude and login), POST /api/setup/run {step: claude | login | chrome | github | access | doctor}, /api/setup/input {step: login, text} (the code from Anthropic's page), /api/setup/cancel {step}, POST /api/setup/bundle (a zip for support, masked); a feed row `out` has news: true when the core counts the result as new for the owner (as the session's owner_out); the page counts "New · N" and the "Last read" line from it; a theme's --radius-pill and --jump-shadow are the "New" mark and the "To the latest" button; POST /api/send takes tasks — the cards of a direction's board the owner named ([{id, title, status}], up to 40; the core puts one line with them into the session's label) — when the snapshot's features have send.tasks, and an in row of a timeline has tasks (the same list); POST /api/send takes app (web | ios | mac | android | telegram) and app_version (up to 20 ASCII letters, digits, space . + ( ) - _), the client named in the owner's label "from user · Android 0.2.7"; anything else is a 400 — when the snapshot's features have send.app; GET /api/feed?plugin=<name>&post=p<seq>[&post=…] — the channel's posts by number, also past the 500-post window (up to 20, read back from the journal; an unknown number or another channel's post is skipped, a bad number gives 400) and hub.feedPosts(ids) on the page, when the snapshot's features have feed.post_by_id; the hub bus on the page (spec hubbus/ux, when the snapshot's features have bus) — POST /api/bus/act {op: view | preview {text} | history {name} (rows: {id, ts, way: in | out, kind: chat | thread, text, status, thread, agent, source, outcome, passed_to, why, note, reply, finding}) | link_text {id} | revoke {id} | cancel {id} | off | chat {name, text, confirm?} (the owner's word to that hub's liaison, an ordinary message; 409 not_verified until «Matches») | verify {name, code?} | people — the records of the owner's contacts book for «Whose hub» ({people: [{name, kinds}]}, the kinds of their paths, no values) | person {name, person} — whose hub it is: person {name: <a record of the book>} | {new: <a name, a record is made>} | null (clears), 409 when the book has none or several; there is no table of rights any more — the liaison decides, `rights` and `expose` are 400 | unlink {name} | rename {name, alias} — the owner's own name for the hub, kept on this hub only; empty — cleared; no control, format or separator characters, up to 100 | name {text} — this hub's own name for the other hubs, the one required setting (the view's `name`, null until given; 400 for a non-string, 409 for a name the core refuses)} (a refusal — 409 {error, code}; a key-like chat text — 409 with confirm: true), POST /api/bus/start {what: link, ttl?: 1h | 24h | 7d, consent?, label? — the hub's name in that invitation alone, up to 100, person? — whose hub, as in person} | {what: join, text, person?} — one job at a time (409 while one runs; the first link of a hub with the bus off without consent: true — 409 {consent: true}; both 409 {code: no_name} while the hub has no name; an open invitation in the view's links has person — the name of the person of the book it was made for — and label — the hub's name in that invitation alone, each null when none), POST /api/bus/job — its steps and outcome (the made link's text only here), the snapshot bus — the contacts for the session list ({enabled, contacts: [{name, label, alias, stated, person, status, verified, code, chat_in, last_in, waiting}]}, null until the first contact; label is what the page shows — the owner's alias when set, else stated, the label the hub gave itself); POST /api/voice answers 413 {error} (the limit and what to do, in the page language) for a recording above the limit, where it answered 400 — 400 stays for an unreadable or empty recording; hub.input(...).record() starts a voice recording in the field from the plugin's own click (the same as the microphone button; true when it started, false when voice is off or unavailable, the field holds a draft or something is recording), when hub.capabilities.inputRecord === 1; out_head of a session in the snapshot has no Markdown marks any more (the core strips `**`, `*`, `` ` ``, links, heading/quote/list marks; it was the text as is); a heading followed by text ends with a full stop in it and the rows of a table after some text are left out ("Report. The map is rebuilt.", not "Report The map… a | b"); GET /api/about has addresses — the hub's own addresses for a client to keep as fallbacks ([{kind: ip | name, url}], the IP first; [] while the tailnet is off), when the snapshot's features have about.addresses; gist of a session in the snapshot is null also when the `hub gist` said earlier in the same turn was replaced by a result with no gist line of its own (show the start of the result, out_head), except for a session that still waits (waiting) or runs a background job of its own that is not a service one; 1.31 (27.10, released in v1.6): a message in the snapshot has inserted_unread — typed into a busy session mid-turn and no turn with its mark has started yet (label "inserted, not read yet", state stays delivered); a session in the snapshot has relay — the mini-hub the owner's words to this executor go to (null otherwise), and an in row of a timeline has relayed — the mini-hub the owner's words went to instead of this executor (not its queue, starts no turn; midturn — its turn was running); POST /api/session/stop {session} — stop an executor at once (its process tree is killed even mid-step, unread lines are withdrawn: failed with reason "stopped by owner"; only an executor, otherwise 409) and a session in the snapshot has stopped — {ts, by: "owner" | the session that stopped it} while a stopped executor sleeps, null otherwise; the snapshot mcp_waits — MCP servers waiting for a login ([{server, since, sessions, fp, host?, confirm?}], the oldest first; since survives session restarts; one name at two addresses is two entries); a sign-in to a waiting MCP server from the page — POST /api/mcp/login {server, fp?, confirmed?} → {attempt, url | done | pending}, POST /api/mcp/link {attempt}, POST /api/mcp/callback {attempt, url} (the address after a sign-in on another device), the snapshot mcp_logins ([{id, server, fp, host?, session, state: asked | open | sent | done | failed, ts, at, error?, pty?}]; the link and the address are never in it); dictation into a field — hub.dictate(field, {onText?(text, {inserted}), onError?(text, {retry?, download?}), space?}) → a microphone button (space: false — Space does not start recording; inserted: false — the field was rebuilt or became read-only, the text is the plugin's); the transcription goes into the field (input, textarea, contenteditable) at the caret with an input event, without sending; hub.capabilities.dictate; in the snapshot delegations has only open tasks — working, waiting-owner, stopped (replied is no longer there; archived was not since 1.30), a session's info keeps only model, effort, version, launcher, harness, backbone, launch_generation, mcp_failed, mcp_auth, updates have no notes, code has no changes and code.unpublished is an empty list (unpublished_count stays) — the full code and updates with notes are in /api/about (its new updates field); the agent-training session (trains) in the snapshot has stops — the stops audit of the agents {ran, check, done, asked, streak, alarm: off | silent | failed | skipped | null}; a session in the snapshot has out_head_ts — the time (event ts) of the same result as out_head; null — there was no result (and for states of an earlier version until the next result); /api/backlog facts — lag, the board lags behind the work ({since, age, delegations, owner}: after the last card edit the lead got the owner's words or raised executors without a card, older than 5 min; no lag — no key); an emoji shown as text (a theme without a picture for it) carries data-emoji with the emoji itself; a theme can set the color "code-bg" — the background of code blocks and a plate behind inline code in the feed and in the text of "Files" (CSS: --code-bg; without it the look is as before); an in feed row has agent — the name of the external agent a core module relayed the message from (the agent field of its message.received fact; only on a module's fact — channel plugin, sender mod:<name>, not the owner; one line, 1-40 characters, without control or invisible characters); subgroups in a session list group — hub.addSubgroups(fn(group, rows, snap) → [{id, title, rows, open?}] | null), the core draws and folds their headers like its own groups (open: false — folded at first, search opens them); hub.groupFold(id, open?) — read or set whether a group, a "Now" section (a:<section>) or a subgroup (<group>/<subgroup>) is open; hub.capabilities.subgroups; a theme can opt the shared composer into full-width multiline text — --composer-wrap: 1 (stacked: the core adds .composer-stacked once the text wraps beside the controls or has a line break, until the draft is empty) | 0 (no stacking, width is still watched), --composer-wrap-right and --composer-wrap-gap (pixels); without --composer-wrap the composer is unchanged; hub.sidebar() → {open, available} and hub.sidebar({open: boolean}) — read or change whether the core left sidebar is open (on a phone available is false and the desktop choice is not changed), hub.on("sidebar", fn({open})) — a change from the divider or a plugin (returns the unsubscribe), hub.capabilities.sidebar; hub.input accepts leading() + leadingSig — a plugin's own node before the action in the composer's bottom row (kept across draft changes, replaced when leadingSig changes, leading: null removes it), hub.capabilities.composerLeading; a theme can set --sidebar-default-width (200–520 px, the core fallback is 272 px) — a width the owner dragged to wins; GET /api/feed no longer waits for the journal lock — it answers with the posts of the latest read-through (like /api/snapshot: a post written a moment ago may appear on the next poll), 503 only while the journal is read for the first time or from the start; a view's toolbar — toolbar(root, snap) in hub.addView: the core keeps root (a .view-toolbar) in the top strip only while the view is open, the same node across snapshots, leaving and returning (an empty root takes no space), hub.capabilities.viewToolbar; a repeated hub.addView with the same id from the same plugin keeps the view's root, the toolbar root and whether it was opened (fields keep text, focus and caret; full is applied to the kept root); 1.30 (23.10, released in v1.5 — 26.10 added to it: POST /api/voice — optional session (the addressee), a transcription failure is 502 {error, id, file, reason, context?, partial?} (error carries no reason or disk path; reason is the cause in the hub's short words, without a path or command output; a hub from before this change has none — do not rely on it), GET /api/voice/failed?id=<id> — the failed recording (owner only, Range; kept up to 7 days and ≤ 200 MB in total — an old file may be 404); the sidebar.tiles place — the top of the sidebar above the search row (earlier than search in the markup and in Tab order; empty means no padding); /api/backlog — facts, core facts about the cards of the direction board (cards {TASK-N: {kind: working | unsorted | unknown | idle, …}}, unlinked — executors without a card within a day, counts); in the snapshot direction_tasks has working, unsorted, unlinked): in the snapshot delegations has no tasks "in the archive" (state archived: a handed-in task of a removed executor after 2 days, fact core.archived; the core-state entry of such a task has context and reply as the beginnings, trimmed); in the core state (core.journal.load_state, Tail.reader) a parked session and a retired executor have state parked and retired (was asleep with a parked or retired field), the page snapshot and the module slice keep the former shape; while the page reads the journal for the first time (or rereads it from the start), the answers that need the core state (/api/timeline, /api/history…, /api/feed, /api/flow, /api/usage, /api/modules, /api/subagent, /api/send/receipt, POST with a session that is not in the still empty snapshot) are 503 with Retry-After at once, a journal lock held longer than 15 s is 503 too; /api/usage, while "Costs" reads the journal archives for the first time (minutes the first time after an update), waits up to 10 s, then 503 with Retry-After; /api/about answers at once (core_started and sessions are null while there is no snapshot from the journal); connections over the limit (core/httpd.py) get 503, a connection silent for 60 s is closed; /api/send without card — the words answer a session's question card only if it is the only open one (with two or more, none; with card — as before); in the snapshot budget has gate (the budget gate: true — executors at the limit wait for the owner's word, false — for the window reset; hub.json budget.owner_gate);/api/answer, /api/permission — the core did not record the answer within 3 s: 202 {ok, queued, text}, the same request with the same client_id asks for the outcome (in the mailbox — queued again without a second fact; the core refused — 409 {error, reason: "refused", seq}; the journal reader is busy — 503); POST /api/accounts/use {account, force?: true} — 200 {ok, active}, 404 not in the pool, 409 {error, until?, forceable} paused or busy (limit, switch threshold; force bypasses only the threshold), "accounts.use" in features, account.forced in the snapshot; /api/plugins/install and /api/plugins/update — an update with new rights without accept: true is 409 {error, wider: [rights in words]}, nothing installed (with accept: true it is installed, the consent is the event plugins.consented); the first install of a catalog entry whose code asks for a hook, an MCP or LSP server, a monitor or bin/ that the catalog did not name is also 409 without accept: true (the page sends it after preview); a plugin in the plugins snapshot has page_access ("full" — it has page code: full access to the page) and market; hub.banner(text) — the hub's words in the page banner (one line, ≤ 300 characters, text only, fade after 10 s, a click closes; it does not override the page's own banners — "no connection", "updated" — nor does it show where no banner is visible; then it returns false), hub.capabilities.banner; GET /api/marketplaces — connected catalogs ({catalogs: [{name, source, official, commit, updated, entries, skipped, problem, error}], entries: [{name, catalog, type: plugin | module, pinned, version?, description?, author?, rights?, revoked?, icon, installed?, newer?, wider?}], official}), /api/marketplaces/icon?catalog&entry — a catalog entry's icon from the catalog clone; POST /api/plugins/install {entry: "<entry>@<catalog>", preview?: true, accept?: true} — a catalog entry exactly at its commit (preview — rights before installing, a core module is 400: it is installed by a command); /api/plugins/update and /api/plugins/remove of a catalog-installed plugin go to the catalog version and through the catalog installer (reviewed — 400), "newer available" (/api/plugins/updates) is not written for such; the same holds for an entry installed before catalogs (without market) whose source is pinned in a connected catalog: in /api/marketplaces the catalog entry's installed is its key; /api/plugins/search — the hub's former list (trusted) only while the official catalog cannot be read, entries of a GitHub account (markets) have legacy: true — without reviewed, /api/plugins/install answers 400 (only through review); deprecated (DEPRECATED, until 2.0): hub market, markets, trusted.json; 1.29 (22.10): the send receipt — /api/send waits for the core's decision (up to 3 s): 202 {ok, receipt: "accepted", seq, id, client_id?} (a repeat of an accepted client_id — already, with the seq and id of the first), a core refusal — 409 {ok: false, receipt: "refused", error, code?, seq}, the core did not decide — 202 {ok, receipt: "queued", queued, text, client_id?}; GET /api/send/receipt?session&client_id — {receipt: accepted (seq, id) | pending (in the mailbox) | refused (error, code?, seq) | unknown}; hub.send — and receipt "queued", hub.receipt(session, client_id), hub.capabilities.receipt;1.28 (21.10): the composer transport — hub.capabilities ({composerTransport: 1, views: 1, openSettings: 1}), hub.send(body) — an owner message with client_id and a receipt (accepted | refused | unknown), hub.input — send(body) (the plugin's own transport), draft() and set({text, files}) (the field draft); the page composer sends client_id, and a retry after an unknown outcome sends the same one; navigation to a view item — hub.go(id, params), params in the address #/x/<id>?k=v; the view receives them in open(root, params, ctx) on entry, a new address, a repeated navigation and a late registration, ctx.from and ctx.close() — return to where it came from; /api/answer, /api/permission, /api/question/dismiss — client_id; the receipt 202 {ok, seq} (the number of the accepted fact); a repeated answer to a closed card — by the same decision or the same client_id 200 {ok, already, seq}, by another — 409 {error, reason: "answered_other", seq}; a session in the snapshot has answered — closed cards [{request_id, seq, sig, client_id?}] (the answer is applied); the view registry — hub.views() ({id, title, icon, order, owner}: "Settings", "Schedule" and plugin views), hub.on("views", fn) — a registry change, hub.openSettings(tab) — open a tab of "Settings" (true/false); 1.27 (19.10): the data-hub-hold attribute on <html> — the plugin holds a hub restart (all, web, ext:*) while it has something undelivered, no longer than 2 h from the request or restart_wait_minutes, if longer (since 05.11); the value is the name for hub status; POST /api/hold {tab, holds}; a tab that holds does not reload itself after an update; 1.26 (18.10): a feed row of in has from {kind, name?}, who sent it (as for a message in the state; for old entries — by the label, without one — owner, core or null); pushes (the "notification reason" spec): the snapshot has alerts — open reasons [{key, id, kind, session?, card?, title, subtitle?, body, since, v}] and alerts_rev, in features — "push.alerts" (while pushes are on); /api/push/register — caps (["clear"] — the client clears the banner with a silent push); the push payload has hub {v, hub, rev, key, kind, card?, n?, closed, open?}, apns-collapse-id = key; 1.25 (17.10): /api/session/effort — default (drop its own level); hub.addUnread — the plugin decides "new" on a session row and "To review", the session in the snapshot has owner_reply (the latest result strictly for the owner) and seen (how far he looked); a session in the snapshot has gist — its gist in one line (hub gist, {text, ts}, ≤ 160 characters; null — none or stale); the page language — hub.lang, hub.locale, hub.t (i18n/ catalogs), GET /i18n.json; owner.lang — also codes uk, ja…; service feed rows have code (created, removed, resumed, compacting, handoff, job — with a job field), schedule jobs have wait — what the hub wait check waits for; in the journal core.rejected has the reason code (core_failure), core messages in the state have from {kind, name}; the texts of labels, reasons and rows are not an API promise (core/forms.py); /api/push/register — the device mode (none — no pushes for it; asks, results, all — while questions); /api/send — client_id (a repeat to the same session with the same id within a day is 202, one message) and todo (the id of an item in the client's to-do list that the words answer — into the message.received data as is, the core does not interpret it); the snapshot commands.list — core commands via "/" ({name, args, hint, path, session, arg, choices?}); /api/file serves a file sent by a session (hub send-file) of any type; what is not a raster image, and sound and video, are download-only (attachment, nosniff, sandbox);1.24 (17.10): a session in the snapshot has out_head — the beginning of the latest result addressed to the owner (≤ 160 characters, one line; null — there has been no result yet);1.23 (16.10): a session in the snapshot has parallel — its own number of executors of a specialist or direction (0 — "solo", null — default), executors_live — how many places its live executors take; at the root parallel is the hub's overall parallelism; POST /api/session/parallel {session, n: integer ≥ 0 | "default"};1.22 (16.10): a session in the snapshot has effort — its own reasoning level (null — default), /api/session/effort accepts effort too; /api/send — card (an answer to the named question card), an attachment not from /api/upload is 400, not silently dropped; a post to a plugin channel — up to 20 000 characters, longer is 400, not truncation; the snapshot upload — an attachment as a raw stream ({raw: "/api/upload/raw", max_bytes, max_inline}), in features — "upload.raw";1.21 (16.10): core modules — the snapshot mods (their own state, {module: {key: {value, rev}}}) and mod_versions, in features — "modules", while at least one is loaded; a page plugin from a module — in extensions with base (/mod/<module>/web/); 1.20 (16.10): the snapshot budget — the limit saving mode with mini-hubs ({mode: economy | owner, why, until, queued} or null); 1.19 (16.10): the snapshot queue — executor requests of mini-hubs waiting in the queue ({name, by, pos, wake, task, why}), a mini-hub whose task is with executors has label — "waiting for executors · N + K in the queue";1.18 (15.10): a session in the snapshot has minihub — a mini-hub (hub parallelism, core/minihub.py), minihub_pending — will become one (what it waits for), minihub_dirty — uncommitted in the mini-hub folder (a mark), solo — the "solo" checkbox, solo_auto — "solo" by itself (why); 1.17 (15.10): a session in the snapshot has status — working | waiting | idle | asleep | failed, one core rule for labels and counters, and terminal — the Claude Code window waits for an answer in the terminal (hub attach); 1.16 (15.10): free groups of the session list — the snapshot layout ({rev, groups, place, order} or null) and own_icon on a session, hub.addOrder receives the ids of its own groups too; 1.15 (13.10): a session in the snapshot has waiting — only the fresh "what it waits for", the stale one (it was written to afterwards) is waited;1.14 (12.10): ctx.open() for the session.aside place — the plugin opens the panel itself; 1.13 (09.10): hub.addCommand — plugin commands via "/" in the session composer; 1.12 (11.10): hub.addOptions / hub.option — plugin checkboxes in the "Settings" tab; 1.11 (09.10): theme — the "attention" color (the header of "session waits for you" cards; CSS: --attention, --attention-line); 1.10 (08.10): hub.addOrder — the order of rows in session list groups, hub.redraw; 1.9 (06.10): theme — "font_scope": "mono" (the theme font for monospace only); 1.8 (05.10): theme — "font_scope": "all" (the theme font on the whole page) and "font_scale" (its scale); 1.7 (05.10): hub.input — a composer like the session chat's; 1.6 (05.10): the session.aside place — a panel to the right of the open session; 1.5 (04.10): the sidebar.header place — the sidebar header; 1.4 (04.10): the session role "executor", raised_by and backlog in the snapshot, a form question is a card in pending, not "dialog"; 1.3 (03.10): theme — own icons, pictures instead of session emoji, a font; 1.2: the plugin channel; 1.1: show/hide, icon, full, addStyle, preset; spec mode (core/specmode.py): a session row has specs_on (a person's /spec on) and spec {task, path, ready} — an executor's open spec, ready: it reported and waits for the owner's "Accept" (status waiting, label "waits for you: spec dN"); a task of the board has spec {path, state: open|accepted|dropped, …} and the new state spec; facts session.spec_set {on} (only channel web|telegram, the owner), delegation.spec_accepted {sha256, pending} and delegation.spec_dropped (the owner, or whoever raised the executor with --for mN); the core's note in the owner's name about his action on a card (an answer, a closed or a withdrawn card): the message has on_behalf "owner", its in feed row sender :core, from {kind: core} and on_behalf "owner" (features feed.on_behalf); hub.messageAt(node) on the page — the message of a feed row for a plugin's own actions on it {session, kind, mine, target (what POST /api/react takes, or null), text}, hub.capabilities.messageAt; limit_cut {ts, resets} in a session's row of the snapshot — the turn was cut by an account limit and the core has not continued it yet (resets: when the window resets, null if the core does not know it; the row is null when not cut); a record of a session's answered in the snapshot has how (answered|dismissed|withdrawn), id (the card's qN or null), ts, by (owner or the name of the session or the hub), why — a card withdrawn by the session that asked or by the hub is there too (sig null; a reply to it is 409 as to a card that is not there, not 'already accepted'); records written by an earlier core lack the new keys; a reason in the snapshot's alerts held for the working morning (hub.json quiet_review) has quiet: true and its since is the release; a page theme may have "measure": "<number>ch" (40 to 200, anything else is ignored) - the longest line of the feed text and of the rendered text in "Files" (--measure; the system theme has 68ch); the core has its own themes: Clean, the default when the browser chose nothing (core:clean as in the system, core:clean-light, core:clean-dark: classes th-clean and th-clean-dark on <html>, its CSS acts only under them) and Classic (core:classic, the earlier look, no class) - hub.theme and files.theme hold {id} for them; a plugin theme lies over Classic as before; the catalog plugin kalimullin:clean is no longer loaded (its choice moves to core:clean-light/-dark); a session row's background in the snapshot can have rows of kind "run" — a `hub run` command the core runs outside the session (core/hubrun.py), with until_at, shown parked too; the events run.requested (a session), run.stop_requested, core.run_started and core.run_ended; the snapshot's voice is also true when the built-in model is installed (GET/POST /api/voice/local — «Settings -> Modules -> Voice», the core's own path); owner.file in the journal can carry screenshot: true — the sender says the file is a screen capture (`hub send-file --screenshot`); a `hub shot` PNG (not --compare) has a tEXt chunk Software = "hub shot" right after IHDR; GET /api/file?path=…&w=<px> (`file.thumb` in features): a raster picture (judged by its bytes) scaled by width to a jpeg - the width is rounded up to 120/240/480/960/1920/2048, a picture not wider or over 64 Mpx, a machine without sips or scalers busy for 2 s get the original (check Content-Type); every /api/file answer carries an ETag (strong; a thumbnail's is weak, W/, of its source and width) and If-None-Match gets a 304; Cache-Control of /api/file is now `private, no-cache` instead of `no-store`; the `in` feed row of a task (`task: true`) has `brief`, [start, end) in UTF-16 units of `text` — where the words of whoever set the task lie, the rest of `text` is the core's frame for the model (the executor's rules, notes); absent in a task of an earlier version or when the text no longer holds the words: show `text` whole; POST /api/accounts/use 409 has `code` (paused | busy) — a client tells the refusal by it — and, when busy, `why`: the cause alone in the hub's language (the window the account waits for: "5-hour limit spent" / "weekly limit spent" at 100%, otherwise its share or the old reason), for "busy until <until> — <why>"; `badge` in addView({...}) (page plugin API): a positive whole number is shown as plain text at the right of the view's ☰ item (anything else shows nothing; register the view again to change it); hub.capabilities.viewBadge === 1; `folder` in GET /api/themes: the folder of this hub's own theme files (`~/…` under the home folder), for the hint of Settings → Look; the page's password (core/pagepass.py, `page.password` in features): without the sign-in /api/* and "Files" answer 401 {error, code: signin} from this machine and from the tailnet or Cloudflare Access alike, GET / is the sign-in form posting to POST /signin, POST /api/signout, GET /api/signin -> {signin}, `signin` in GET /api/about (open|password|key|broken; the short card without the sign-in has it too), X-Hub-Local on POST /api/secrets/fill, POST /api/instances password|password_off and remove {password}, `signin`, `password_ok` and `unprotected` in GET /api/instances; GET/POST /api/page-key are gone; `reply_head` and `reply_ts` in a session row of the snapshot - the start (<= 160 characters) and the time of the latest answer to the owner (`owner_reply`), kept when a later result the session sends on its own replaces `out_head` (null - no answer yet or a state of an earlier core)
DEPRECATED = {   # feature name -> {"since": "1.x", "removed": "2.0", "use": the catalog key of "what to use instead"}
    "hub market": {"since": "1.30", "removed": "2.0", "use": "core.version.use_market"},
}
# the Claude Code this core release is tested on (owner decision 2026-11-11: only the latest one at the time of the
# release): publish.sh writes the stable channel's version here in every snapshot (tools/compat.py --pin), the release's
# stands test it, and the hub runs exactly it from its own folder (core/ownclaude, spec d2263524: the update downloads
# it without a model; the person's claude is not touched). The working copy names none ("": not a release — a hub from
# it runs claude from PATH as before, spec decision R1). The line is CLAUDE_TESTED, not MIN_CLAUDE = "<version>": an
# updater from before 2026-11-11 reads that literal of the release it is about to install and, with an older claude,
# waits for a person to update it ("needs-claude") or runs `claude install`. Read the version of a release's file with
# core/ownclaude.named()
CLAUDE_TESTED = "2.1.311"
CLAUDE_FLOOR = "2.1.218"   # the oldest claude a hub from a working copy (no CLAUDE_TESTED) counts as ready («Setup»)
MIN_CLAUDE = os.environ.get("HUB_TEST_MIN_CLAUDE") or CLAUDE_TESTED or CLAUDE_FLOOR   # the tests' own (tests/__init__.py)


# The app (core/packaged.py, format "app"): the number of the environment (Python, Node, git, gh, ffmpeg…) the app shell
# carries is "runtime" in its PACKAGED; this code needs at least RUNTIME_MIN. Raise it in the release whose code needs a
# newer shell (a new Python minor, a new tool in it): modules/update holds such a release ("needs-runtime") and the page
# gives the link to a new app. The update reads the number from the target release's file, as CLAUDE_TESTED.
RUNTIME_MIN = 1


def _ver(s):
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?", s.strip())
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def beyond(want, api=PLUGIN_API):
    """The plugin needs an API newer than api (09.10, owner decision: such plugins are not released to the catalog)."""
    need, have = _ver(str(want or "").strip().lstrip("^>= ")), _ver(api)
    return bool(need and have and need > have)


def compat(want, api=PLUGIN_API):
    """(state, text for a human): ok | unknown (no version given) | newer (needs a newer one — update the hub) |
    incompatible (another major version — the plugin does not load)."""
    from core import i18n   # lazily: this module is imported early (the page server, tools) and has no imports of its own
    have = _ver(api)
    if not want:
        return "unknown", i18n.t("core.version.unspecified", api=api)
    w = str(want).strip()
    need = _ver(w.lstrip("^>= "))
    if need is None:
        return "unknown", i18n.t("core.version.unclear", want=w, api=api)
    if need[0] != have[0]:
        return "incompatible", i18n.t("core.version.older" if need[0] < have[0] else "core.version.newer_hub",
                                      major=need[0], api=api)
    if need[1] > have[1]:
        return "newer", i18n.t("core.version.needs_newer", major=need[0], minor=need[1], api=api)
    return "ok", ""


def deprecated(features):
    """Warnings about the obsolete features a plugin uses."""
    from core import i18n
    return [i18n.t("core.version.deprecated", feature=f, since=DEPRECATED[f]["since"], removed=DEPRECATED[f]["removed"],
                   use=i18n.t(DEPRECATED[f]["use"])) for f in features or [] if f in DEPRECATED]


# The hub's release number: the tag vX.Y.Z of the shared repository, put by tools/publish.sh --tag on a published
# snapshot — the mark of a packaged release. The code files hold no number — a release tags what is already published.
RELEASE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def channel(cfg):
    """The hub's update channel. A hub from git has one, main (nightly: the head of update.branch; the owner's decision
    of 2026-11-18, m2571664: no installing by tags from git) — whatever update.channel says, so a hub that was on
    stable moves to main by itself with the update that brings this code. Only the app's clone (update.app, set by
    core/packaged.apply) follows release tags."""
    up = cfg.get("update") if isinstance(cfg, dict) and isinstance(cfg.get("update"), dict) else {}
    return "stable" if isinstance(up.get("app"), dict) else "main"


STABLE_DELAY_HOURS = 24   # the app clone's delay: a tag is offered no earlier than this many hours after it is first seen


def delay_hours(cfg):
    """The delay of a new release in hours (update.delay_hours) for the app's clone on release tags: a number from 0; no
    setting or an unclear one means 24; a hub on main has none. "Install" and `hub update` do not wait for it."""
    if channel(cfg) != "stable":
        return 0
    value = cfg["update"].get("delay_hours")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return value
    return STABLE_DELAY_HOURS


def browser_tests(cfg):
    """Whether the update self-check runs the browser tests (update.browser_tests; owner 03.11: off by default, can be
    turned on): only true turns them on, anything else ("abc", "1", null, a broken "update") is the default."""
    up = cfg.get("update") if isinstance(cfg, dict) else None
    return isinstance(up, dict) and up.get("browser_tests") is True


def release_number(name):
    """(X, Y, Z) of a release tag "vX.Y.Z" or None (v1.2, v1.0.0-rc1 are not releases)."""
    m = RELEASE.match(name or "")
    return tuple(map(int, m.groups())) if m else None


def release(repo, state_dir=None):
    """The newest release already in the hub's code (the tag is an ancestor of HEAD): "v1.2.0"; None — none (or not
    git). The tags are releases of the shared repo seen by modules/update (refs/hub-update/tags: it removes a withdrawn
    tag with --prune) and the clone's own tags (refs/tags) that the updater has never seen: a release fetched and merged
    by hand while updates are off (05.11: the updater's tags stay as they were, "About the hub" named the old release).
    A tag the updater saw (state_dir/update/seen.json, it keeps them all) and no longer has was withdrawn: not counted;
    without state_dir there is no such record."""
    def tags(ns, merged=True):
        try:
            r = subprocess.run(["git", "for-each-ref", *(["--merged", "HEAD"] if merged else []),
                                "--format=%(refname:lstrip=-1)", ns], cwd=repo, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.split() if r.returncode == 0 else None
    fetched = tags("refs/hub-update/tags/", merged=False)
    if fetched is None:
        return None
    known = set(fetched)
    if state_dir:
        try:
            with open(os.path.join(state_dir, "update", "seen.json"), encoding="utf-8") as f:
                seen = json.load(f)
            known |= set(seen) if isinstance(seen, dict) else set()
        except (OSError, ValueError):
            pass
    names = (tags("refs/hub-update/tags/") or []) + [n for n in tags("refs/tags/") or [] if n not in known]
    found = [(release_number(n), n) for n in names if release_number(n)]
    return max(found)[1] if found else None

# Module API (the Module API spec, section 3): the contract with core modules — module.json, the points the core calls
# on a module's process (core/modrun.py), facts into the mailbox. Its own line, separate from PLUGIN_API: modules of
# another line (^1.x — in the core process) are not loaded here. Every release with a new point is the next minor (the
# number is assigned at release, not during development); within 2.x the shape of points does not change, replacing a
# point means a new point and DEPRECATED on the old one, removing — only in 3.0.
MODULE_API = "2.10"
# the Embed API (core/embed.py): what the core promises a company's code its build puts into embed/<name>/ — the slots,
# the names the core calls of them, the packages and files it looks into. A minor number adds (an added name is optional
# for an embed); a major one changes or takes away
from core.embed import API as EMBED_API  # noqa: E402 — one number, core/embed.py keeps it
CODEX_POINTS = ("codex.list", "codex.decide", "codex.use", "codex.add", "codex.remove")   # optional, see OPTIONAL_POINTS
# points of each version: a module declares in hello only the points of its own version and older (a module with an
# api above the hub's is not loaded); the module's own version is the lower bound of its api (module_floor)
MODULE_POINTS = {
    "2.0": ("hello", "migrate", "launch.env", "cli", "doctor"),
    "2.1": ("prompt.parts", "prompt.input"),   # session prompts (core/modprompt.py)
    "2.2": ("fold", "actions"),   # core.write: intercepting event handling and editing actions (core/corewrite.py)
    # developer mode: hubmod with decorators, modules.dev, trusted sources, a view calls its own handles, 256 KB of
    # its own state — no new points, a module that needs this writes ^2.3
    "2.3": (),
    # a module for a client with another API (core/modclaims.py): own /api/… paths and snapshot keys — no new points
    "2.4": (),
    # owner.notify with id, session, push and owner.notify_resolved {id} — withdraw one's own notification (the push
    # spec): an event, not a point — a module without core.write can write it, like owner.notify
    "2.5": (),
    # hubmod.state gives and takes a copy, owner() with a bad dedup_key — HTTPError 400, a repeated dedup_key — code
    # duplicate, a dev module's log is at most 4 MB; hubmod.fact(dedup_key=, wait=) — the dedup field of a module event;
    # hubmod.servable as a list and servable_state; a wider module slice (core/modstate.views): session row fields
    # (the sessions right), the accounts and messages rights — no new points
    "2.6": (),
    # the model right — a short model call through the core (hubmod.model, core/modelcall.py; the owner allows it:
    # modules.model) — no new points
    "2.7": (),
    # session login (core/profiles.py, the profile right): which profiles exist, when to switch, the login environment,
    # a manual switch; the account.limits fact from the login module
    "2.8": ("profile.list", "profile.decide", "profile.token", "profile.use",
            "speech.say"),   # reply voice (modules/voice/speech.py, the speech right)
    # a transport of the hub bus: the bus.transport right — the facts bus.offered and bus.handed (core/busstate.py) and
    # the transport interface files (core/busd.py) — no new points
    "2.9": (),
    # Codex (experimental): the Codex account pool (core/codexslot.py, the profile right; a tree without its code does
    # not offer them) — which accounts and folders, when to switch, a manual switch, a new account's folder, removing an
    # account; hubmod.model (core/modelcall.py) no longer follows a model remap the sessions have (ANTHROPIC_DEFAULT_*_MODEL and the like from user_env: core/catalog.service_env) — haiku is haiku;
    # a module without its own process (no run, only the page plugin: core/modpages.py, NOPROC_SINCE below) — no points;
    # the queue.pause right (MODULE_MANIFEST): task.queue_paused / task.queue_resumed from the module — facts, not points;
    # the message.rush right (MODULE_MANIFEST): an urgent message (`now`) from the module, typed in mid-turn — a fact
    # (message.received), not a point; an urgent task from a module is still refused;
    # the sessions slice row: limit_cut {ts, resets}, raised_by, since, worktree {branch, base} (no paths)
    # the cli point: caller of a schedule job's command or check also has schedule_job (the job's id) — a field, not a point
    "2.10": (*CODEX_POINTS,
             # admission of an executor's launch (core/daemon.py: admit_pass, the task.admit right): {run} -> {hold: reason} | {}
             "task.admit"),
}
# optional points: a module may declare them in hello with a lower api floor than their version — it declares one only
# when the core calls it (core/modrun.callable_points()), so the same module version runs on a core without them (its
# other points as before); a core before 2.10 has no such rule and refuses an unknown point, which such a module never
# declares there
OPTIONAL_POINTS = CODEX_POINTS
# a module without its own process (no `run`, only the page plugin `web`): its manifest is read since this version —
# a core before it refuses it (the api range above its own, and no run). Written into the same unreleased 2.10
NOPROC_SINCE = "2.10"
# module.json fields and rights that 2.0 lacked, and since which version (a module of a lower own version is refused)
MODULE_MANIFEST = {
    "2.1": ("http", "web", "prompt"),   # handles and views (modules/web/modproxy.py), page plugin, the prompts right
    "2.2": ("core.write", "fold", "fold_state", "actions"),   # core/corewrite.py
    "2.4": ("http.paths", "http.views", "snapshot",   # core/modclaims.py
            "voice.transcribe",   # hubmod.transcribe — the hub's voice provider
            "sessions"),   # the hub's sessions in the module slice (core/modstate.session_rows)
    "2.6": ("accounts", "messages"),   # account limits and owner messages in the module slice (core/modstate.views)
    "2.7": ("model",),   # a model call through the core (hubmod.model, core/modelcall.py)
    "2.8": ("profile",   # session login: the profile.* points (core/profiles.py)
            "speech",    # reply voice: the speech.say point (modules/voice/speech.py)
            "stats"),    # the hub's stats report instead of the built-in one (core/providers.py)
    "2.9": ("bus.transport",),   # a transport of the hub bus: bus.offered, bus.handed (core/busstate.py)
    "2.10": ("task.admit", "queue.pause", "message.rush"),   # admission of an executor's launch (task.admit point, core/daemon.py: admit_pass); the queue.pause right: a module's own pause of the queue (task.queue_paused / task.queue_resumed)
}

_VER = re.compile(r"\d+(?:\.\d+){0,2}")
_CLAUSE = re.compile(r"(\^|>=|<=|>|<|==|=)?\s*(\d+(?:\.\d+){0,2}|\*)")


def module_points(api=MODULE_API):
    """All Module API points up to and including version api."""
    have = parse_version(api)
    return tuple(p for v, pts in sorted(MODULE_POINTS.items(), key=lambda x: parse_version(x[0]))
                 if parse_version(v) <= have for p in pts)


def module_floor(want, api=MODULE_API):
    """The API version a module was written for: the lower bound of the api range ("^2.1" -> "2.1.0"); without one
    ("*") the hub's version. An unclear range gives the hub's version: such a module does not load anyway."""
    lows = [b for op, b in (_parse_range(want) or []) if op in (">=", ">", "==")]
    return ".".join(map(str, min(lows))) if lows else api


def manifest_since(name):
    """Since which module API version the field or right name exists (MODULE_MANIFEST); from 2.0 or unknown — None."""
    return next((v for v, names in MODULE_MANIFEST.items() if name in names), None)


def parse_version(s):
    """"1", "1.2", "1.2.3" -> (1, 2, 3) padded with zeros; anything else -> None."""
    s = str(s or "").strip()
    if not _VER.fullmatch(s):
        return None
    parts = [int(x) for x in s.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def _bounds(clause):
    """One range condition -> [(op, version)] or None. "1" is any 1.x, "1.2" any 1.2.x, "1.2.3" exactly that one;
    "^1.2" is from 1.2 within 1.x (below 1.0 — within the same minor, as in semver)."""
    m = _CLAUSE.fullmatch(clause.strip())
    if not m:
        return None
    op, raw = m.group(1) or "", m.group(2)
    if raw == "*":
        return [] if not op else None
    v, n = parse_version(raw), raw.count(".") + 1
    if op == "^":
        upper = (v[0] + 1, 0, 0) if v[0] or n == 1 else (0, v[1] + 1, 0)
        return [(">=", v), ("<", upper)]
    if op in ("", "=", "=="):
        if n == 3:
            return [("==", v)]
        upper = (v[0] + 1, 0, 0) if n == 1 else (v[0], v[1] + 1, 0)
        return [(">=", v), ("<", upper)]
    return [(op, v)]


def _parse_range(spec):
    out = []
    for c in str(spec or "").split(","):
        b = _bounds(c) if c.strip() else None
        if b is None:
            return None
        out += b
    return out


def valid_range(spec):
    return _parse_range(spec) is not None


def has_floor(spec):
    """The range names a lower version ("*" takes any — it counts)."""
    bounds = _parse_range(spec) or []
    return not bounds or any(op in (">=", ">", "==") for op, _ in bounds)


_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b,
        "==": lambda a, b: a == b}


def satisfies(ver, spec):
    """Is version ver in range spec (">=1.2,<2", "^1.2", "1.2.3", "1", "*")? An unclear version or range — no."""
    v, bounds = parse_version(ver), _parse_range(spec)
    if v is None or bounds is None:
        return False
    return all(_OPS[op](v, b) for op, b in bounds)


def module_api(want, api=MODULE_API):
    """(loads, reason): a module with API range want on this hub. Besides the range itself, the major digit of the lower
    bound must match: ">=1.0" does not load on hub 2.0 — something was removed in 2.0."""
    from core import i18n
    bounds = _parse_range(want)
    if bounds is None:
        return False, i18n.t("core.version.module_unclear", want=want, api=api)
    if not bounds:
        return True, ""
    lows = [b for op, b in bounds if op in (">=", ">", "==")]
    need = min(lows) if lows else (0, 0, 0)
    have = parse_version(api)
    if satisfies(api, want) and need[0] == have[0]:
        return True, ""
    if need > have:
        return False, i18n.t("core.version.module_needs_newer", major=need[0], minor=need[1], api=api)
    return False, i18n.t("core.version.module_old", major=need[0], api=api)
