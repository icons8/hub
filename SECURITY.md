# Security policy

## Read this first: status and honest limits

Hub by Icons8 is **experimental software**. It has not had an external security audit. Use it at your own risk, on machines
and accounts where you accept that risk. The licence (Apache-2.0) comes with no warranty.

The hub starts Claude Code sessions and, by design, runs them in `bypassPermissions` mode: a session can read and write
files, run commands and use every tool and MCP server you gave it, without asking for each action. Whatever a session can
do, the person running the hub can do, so **treat the hub like a shell on your machine**.

The hub adds gates (risky tools need your approval, only your words count as the operator's, and so on). They make
mistakes and manipulation less likely. They are **not a sandbox**. The sections below say exactly where the line is.

## Threat model, in plain words

What we protect: your machine and user account, your files and credentials (MCP keys, Claude login, signed-in browser
profiles), your conversations (journal, attachments), and every hub that updates itself from this repository.

Who we expect to try something:

- **Text written by someone else.** A web page, an issue, an email, a README or a file that a session reads and repeats.
  A hidden instruction in it may try to steer the session. This is the most likely path, and the reason the hub keeps its
  own checks and does not rely on the model's judgement alone.
- **A website open in your browser.** It can try to reach the hub page on `127.0.0.1` with requests your browser sends.
- **Another user or process on the same machine** (a shared server, a container on the host network, a reverse proxy).
  It can reach `127.0.0.1`.
- **Another device on your private network**, once you turn Tailscale access on (see "Remote access over
  Tailscale").
- **A stranger in Telegram**, before the bot is paired with you; an admin of a Telegram group you bind the bot to;
  a person who writes to you privately while the bot answers your chats.
- **Another hub on the hub bus**, and the relay that carries its messages, once you turn the bus on.
- **The code in a session's folder**: a repository you clone, a branch you check out, a project someone sent you.
- **Someone who could change the code you update to**, such as a compromised maintainer account.
- **The author of a plugin, module or marketplace entry** you install.
- **A session itself, once it has been steered** by any of the above, using the `hub` command, the spool folder, the
  schedule and the state files.

## What is NOT a security boundary

Be clear-eyed about these. If your threat needs one of them to hold, the hub is the wrong tool.

- **One operating-system user.** The hub, its sessions and the `hub` command all run as the same user. A session can do
  anything that user can, including editing the hub's own files.
- **Sessions in `bypassPermissions`.** There is no per-action prompt inside a session, and the hub does not sandbox it.
- **A hub on Codex (OpenAI).** The engine is a setting of the whole hub, and everything above holds the same: a session
  runs as your OS user with `approvalPolicy: never` and `sandbox: danger-full-access` — no prompt per action and no
  sandbox. What leaves the machine goes to OpenAI through the Codex sign-in: the sessions' prompts, their context (files
  and command output a session reads, the folder's instructions) and answers, and the hub's own short model calls (the
  watchdog's "does it wait for you", push texts). The hub keeps Codex's sign-in, settings and conversations in its own
  folder, not in your personal `~/.codex`. Other session modes (`default`, `acceptEdits`, `plan`, `dontAsk`) map to Codex's
  approval policy and sandbox and ask or confine as Codex does; a command you approve runs outside the sandbox. The core's
  bans stand in Codex's hook: if Codex does not trust the hook, the session does not start. On Windows the modes that need
  Codex's sandbox set it up themselves, and a session that cannot get it does not start. Masking of secrets in the journal
  is the hub's; it does not apply to what Codex sends to OpenAI.
- **"Operator" and "hub" checks inside the hub.** The core checks who is speaking, accepts only the operator's words for
  operator-only actions, and refuses forged facts from sessions. Within one OS user this is a convention that catches
  mistakes and most manipulation, not a wall. A process that does not carry a session name (`HUB_SESSION`) speaks as the
  operator, as your terminal does; a session can unset or change that variable, or write to the state folder directly.
  The command of a scheduled job runs with the name of whoever wrote its command: a job a session set speaks as that
  session (a plugin's, as that plugin), a job you set speaks as you, and changing only its time keeps whose it is. A
  plugin's command job saved by an earlier release cannot be told from yours and runs as you until it is saved again.
  Session tokens and a real sandbox are not part of the hub today.
- **The page and "Files" have no sign-in unless you set a password.** On this machine they answer only requests from
  `127.0.0.1` with a local `Host` header (against DNS rebinding; the tailnet and Cloudflare Access are below), refuse
  requests that a browser sends from another site, refuse requests that a proxy or tunnel passed on (see "What is still
  open"), and require a marker header on every change request. That keeps other websites out. Without a password it does
  **not** keep out another user or a container that can open `127.0.0.1:8787` on the same machine: whoever reaches that
  port acts as the operator on the page, and "Files" shows the whole disk as the hub's user to a local request. Install
  the hub only on a machine where you are the only user, or set a **password** for each hub (Settings → General → Hubs
  on this computer, "Password" in the hub's row, or `hub password set`; off by default): then the page and "Files" open
  only after it, from this machine and through the tailnet or Cloudflare Access alike. The password is kept as a salted
  PBKDF2-SHA256 hash in a file your OS account alone reads; the sign-in form allows five wrong tries, then waits a
  minute, doubling up to an hour; a browser keeps its sign-in for 30 days of disuse, and a new password signs every
  device out. Limits: a password holds only when **every** hub of your OS account on the machine has one — a hub without
  it opens the others (its "Files" read the whole disk, its sessions run as you), and the list of hubs says which; a
  cookie reaches every server on `127.0.0.1` whatever its port, so a page another user serves on this machine that you
  open in your browser receives your sign-in — open on `127.0.0.1` only what you trust, and never type the computer's
  password into a hub's sign-in form; through Cloudflare the password passes Cloudflare's edge in the clear, like the
  rest of the page; whoever acts as your OS account (your sessions included) is past it anyway; the update's watchdog
  sees, without signing in, whether the hub is up and on what code. Forgot it: `hub password off` in a terminal of the
  machine.
- **Remote access over Tailscale (off until you turn it on).** The hub does not install Tailscale and does not listen on
  the tailnet by itself: the Tailscale switch in Settings → Modules, or `"tailnet": {"enabled": true}` in `hub.json`,
  turns it on. A hub that updates from an earlier release keeps it on if Tailscale worked there — by the old release's
  last check or the new one's first — and the update writes that key once, unless you have already set it. If Tailscale
  is installed but not connected, not answering or failing at the update (a laptop away from its tailnet), the choice
  waits until it connects. A hub without the Tailscale program, or where nobody would be let in (a tagged machine with
  no `tailnet.owners`), stays off and does not turn itself on again at a later update, after a rollback too; an earlier
  release you roll back to turns it on by itself while it runs, as it always did, unless `hub.json` has
  `"enabled": false`. Then, while the machine is connected to a tailnet, the page and "Files" also listen on its tailnet
  address, and a request from there is let in only when `tailscale whois` says the device belongs to a listed login:
  `tailnet.owners` in `hub.json`, or by default the Tailscale login this machine is signed in with — so every device of
  that login gets the page, and "Files" with the project folders (not the whole disk). A tagged machine lets nobody in
  until `tailnet.owners` names someone; tagged devices never count. Tailscale is then part of your trusted base.
  Settings → Modules shows whether it is on and its addresses, and `hub status` says why it is off. The same switch, or
  `"tailnet": {"enabled": false}` in `hub.json`, turns it off, no restart needed: while the tailnet part runs, it sees
  the change within seconds and the page and "Files" stop letting tailnet devices in. The page decides by that part's
  last record, so while the part is not running the last record stands.
- **Remote access through your own Cloudflare Tunnel (off by default).** With `hub remote cloudflare on`, a request
  from `cloudflared` on this machine is let in only with a Cloudflare Access token the hub checks itself: RS256 under
  your team's keys, your team as issuer, your application's audience, not expired, and an e-mail from your list or the
  Client ID of a service token from your list. Unsigned headers and the `Host` are never taken as proof, and the same
  token from a tailnet or LAN address counts for nothing. Such a person gets what the owner from the tailnet gets: the
  page, and in "Files" the project folders, not the whole disk. Cloudflare terminates TLS at its edge and sees the
  page's traffic in the clear (messages, session answers, files you open); Cloudflare Access and your Cloudflare
  account are then part of your trusted base. A leaked Access token works through Cloudflare until it expires (the
  session length of your Access application): keep that short. An open page stream is checked again every minute.
  `hub remote cloudflare setup` makes the tunnel, the DNS name and the Access application itself with a Cloudflare API
  token you paste in your own terminal (not from a session): the token is used for that one command and never written;
  `remove` and `rotate` ask for it again. The hub keeps the tunnel's token (it lets whoever holds it serve your hub's
  name, not read your hub) and a service token's secret in `cloudflare-secrets.json` next to `hub.json` (0600), and
  runs the `cloudflared` build pinned by its sha256 as a child of the hub. A copy of the hub's folders does not run
  the tunnel.
- **The agents' Chrome.** A local browser for agents (`browsers` in `hub.json` with `"kind": "local"`) listens for
  the DevTools protocol on `127.0.0.1` (`127.0.0.1:9230` unless its entry names another port), with no key; the macOS
  installer of a hub from git sets up one on `127.0.0.1:9223`, meant to hold your own sign-ins. Any program on the
  machine, of any user, can connect to such a port and drive the browser: open pages with the sign-ins its profile
  holds, read them and run scripts in them. The page's password does not cover this port. On a machine with other
  users, keep sign-ins you care about out of a local agents' browser.
- **The session's folder is trusted code.** A session runs what its folder declares, with no trust prompt, on Claude
  Code and on Codex alike: the hooks of `.claude/settings.json` and `.claude/settings.local.json`, the MCP servers of
  `.mcp.json`, and it follows the folder's instructions (`CLAUDE.md`, `AGENTS.md`, skills). Starting a session in a
  folder — a fresh clone, someone else's branch checked out for review, a project from a stranger — runs that code
  as you. Review a branch you do not trust from its diff or in a sandbox, not with a session inside it.
- **An HTML report in "Files".** An `.html` file opened in "Files" is shown as a page (`/render/…`) in a sandboxed
  frame: its scripts run with an opaque origin, so they reach neither the hub's API nor its cookies, but they may load
  from and send to any address on the internet (live reports need it). With a password set, the report's address
  carries a mark that stands in for the sign-in for that report's folder and everything below it, for the day it was
  opened and the next; the report's script can read that address and hand it on, and whoever then holds it reads those
  files from this machine (or as you through the tailnet or Cloudflare) without the password. A report in your home
  folder opens all of it that way. Open as a page only HTML you trust, and keep reports in a folder of their own.
- **Page plugins.** Code a plugin adds to the web page runs with the same access as you have on that page: it can send
  messages to any session, approve tools and read what the page shows. The manifest declares this and the hub says so when
  you install or update the plugin, but there is no isolation yet.
- **Plugins, skills, MCP servers, modules.** They run with your account's access. Only install what you have read or trust.
- **Your own account.** The measures below guard against other users, other websites and other devices. They do not guard
  against software that already runs as you.

## What the hub does to protect you

- **Updates in a separate copy.** The hub checks the update source about once an hour. The merge with your local commits
  and the tests run in a separate working copy; your hub's own folder moves only by a fast-forward to the tested result,
  and only forward in history: a rewritten history and a rollback to an older release are refused.
  A hub from git follows `main` (nightly): every hub from git installs a new commit of `main` within the hour, as soon as
  its tests pass; a hub from a package installs released packages only. `update.enabled: false` turns updates off.
- **Secrets stay out of the journal.** MCP servers are recorded without their environment and headers, well-known token
  shapes in tool calls and results are masked (`[masked]`), and `hub journal scrub` cleans archives an older hub wrote.
- **A vault for passwords and keys (Settings → Secrets).** The values are sealed with AES-256-GCM; the key lives outside
  the hub's folder (the macOS Keychain, a DPAPI-sealed file on Windows, a file `0600` in the settings folder on Linux —
  so a backup of your whole home on Linux carries both). An agent uses a value without getting it back: into a password
  field on the sites you named (`hub secret fill`), or into a command's environment for secrets you allowed into
  commands, with the value masked in the output (`hub secret exec`). The page shows no value in the list or in the edit
  form; its "Copy" hands the value to your browser (each copy is recorded in the journal, without the value), from this
  machine or through the tailnet only — through Cloudflare Access the page neither sends nor receives a value. Whoever
  reaches the page as you (see the page's sign-in above) can copy any value. This keeps values out of agents'
  conversations, the journal, screenshots and copies of folders; it does not stop a session set on getting one (it runs
  as your user and can read the field, the key or the page's API), nor a page plugin, which acts with your rights on the
  page anyway.
- **Private files.** The hub sets a restrictive umask. State and settings folders are readable only by you (`0700`, files
  with secrets `0600`), and permissions of existing folders are tightened on start (`hub perms restore` undoes it). On
  Windows the profile folders are protected by ACLs.
- **Keys off command lines.** Any user of the machine can read every process's command line (`ps`). The hub passes its
  own keys through private files, pipes or the environment instead: a session's environment in the tmux host, the
  headers of a remote browser (a `0600` config file for chrome-devtools-mcp 1.10 or later; an older one connects without
  them and the remote refuses it), push notifications. Its temporary copies live in private folders, not under fixed
  names in the shared temp folder.
- **Browsers without keys.** Chrome and chrome-devtools-mcp, which load pages from anywhere, start without variables
  that look like secrets (values you set in `browser.env` stay).
- **Who speaks.** Facts from sessions are checked by the core before they are accepted, and a task, a permission or a
  decision "from the operator" needs the operator's actual words: the page, the paired Telegram chat or an admin of the
  bound group, your terminal; on a hub in `hub bus mode remote`, a linked hub also gives tasks as you would (see "The
  hub bus").
- **Gates on risky tools.** Tools in the catalog carry a risk label (money, production, your data, outgoing messages);
  a session asks for one, and the risky ones connect only after your yes. A plugin is also risky when it runs code —
  a hook, an MCP or LSP server, a monitor, a program in `bin/` or in a skill, a ChatGPT app: the hub reads that from
  its files, not from its author's words, and your card names what will run.
- **Telegram (off by default): your private chat, or one group.** The bot obeys only the account that paired with it,
  and only in its private chat with the bot. Pairing: `/start` to the bot gives a code that lives 10 minutes, and
  `hub-telegram pair <code>` in a terminal on the hub's machine binds the one who asked for it and names them;
  `hub-telegram pair`, `token` and `group` refuse to run from a session, a plugin or a module, and `pair` from a
  schedule job too. Instead of the private chat the bot can serve one Telegram group. Before the bot has an owner, an
  admin's `/start` there gives the code, and `hub-telegram pair` shows the group, its creator, its admins and who may
  appoint admins and binds it only on your yes. That question runs only in your terminal (over ssh — `ssh -t`), not from
  a pipe, a hub session or a schedule job, and so does `--yes`, which skips it. Once the bot has an owner, such a
  request gets no code: it comes to your chat as a card naming the group and its admins, and only your press (or one by
  an admin of the group already bound) moves the bot. Then **every admin of that group speaks for you** — checked with
  Telegram on each message and press, so whoever is made an admin gets your hub — while other members only ask (their
  words are not yours) and use the read-only commands; the bot refuses a group where it may itself appoint admins. Bind
  a group only if you would give each of its admins your terminal.
- **Your chats with people in Telegram (off until you connect them).** Telegram → Settings → Chat automation connects
  the bot to your own account (the bot needs Business Mode). People who write to you privately then reach a session as
  their words, never yours, and a session may answer them in your name without asking you, by the rules you gave it: the
  core lets one answer go to one message, only within a day of the person's message (Telegram's rule), and never a text
  that looks like a key or a password. Only a connection of the paired account counts, and none while the bot serves a
  group. Disconnect the bot in Chat automation to stop it.
- **The hub bus (off by default).** `hub bus on` lets this hub talk to other hubs you link it with. Another hub can
  write to yours only after you add it (its card, or a link one of you made; a pair code you compare with its owner
  checks the key), and each message is encrypted for the receiving hub's key before it leaves, so the relay that carries
  it never gets the text — it does see which hub writes to which, when, and the size rounded up, and it can delay or
  drop a message. Another hub's words reach the liaison session as data, never as yours; the liaison passes them to a
  session or answers, and the core checks every outgoing text for key-like secrets and its size. What goes out is the
  liaison's judgement under your rules (`hub bus rule`): a liaison steered by text it read can send another hub what it
  can read. On a hub nobody sits at, `hub bus mode remote` (yours alone to set) makes the linked hubs whose pair code
  was checked give the hub session tasks as you would; the gates that need your own word (risky tools, installs, the bus
  settings) stay closed to them.
- **Off by default.** Telegram, voice, the hub bus, Tailscale access, Cloudflare remote access and usage statistics are
  off until you turn them on. On from the start: the update check and the daily refresh of the plugin catalogs from
  GitHub (see "A catalog pins code" below; `hub marketplace remove <name>` takes one off for good).
- **No telemetry unless you switch it on.** The hub does not report usage or anything about you anywhere by default.

## What is still open

We list these so you can decide with open eyes. They are known and accepted for now, not hidden.

- **A harmful release that passed the repository's own protection will run on your machine.** There is no release
  signature. Commits to `main` and releases are made only by the maintainers, but the tests that run before the switch
  come from the same code: a malicious release can pass its own tests. A compromised
  maintainer account is the way this happens. If that is unacceptable for you, turn updates off and update by hand
  (`update.enabled: false`).
- **A hub from the one-line installer trusts the project's GitHub Pages site and GitHub releases.** The installer, the
  list of releases with each file's sha256 and the release files all come from there over HTTPS, and the hub checks a
  download against the sum those same pages give: no key held apart from them is involved. Whoever can publish the
  pages or the release files (the maintainers, or someone who takes over their GitHub account) can ship code that
  every such hub takes at its next update (a `.deb` package does not update itself: a new package does).
- **A hub from git takes any commit of `main` within the hour, with no hold.** A commit to `main` does not wait for the
  release test benches, only for the hub's own tests. That is its purpose; install from a package (the one-line
  installer) for released versions only.
- **The mask for secrets is narrow.** It recognises well-known shapes of keys, not every secret. A secret in an unusual
  format, or one a session writes in its own words, can reach the journal. Copies the hub does not control (Time Machine,
  other backups, folder sync, Claude Code's own transcripts) are not cleaned.
- **"Who speaks" can be forged within one OS user** (see above): a process that takes `HUB_SESSION` of another session, or
  none, is believed in the way the core can check, which is not cryptographic.
- **The page has no sign-in unless you set a password** (see above). It is built for a machine with one user.
- **Sessions and scheduled jobs inherit the hub's environment.** Whatever variables the hub was started with (keys
  exported in your shell profile, say) reach every session and job; start the hub from an environment without keys
  you do not want sessions to have. Arguments you put in the command of an MCP server yourself are on its command line,
  where `ps` shows them: give a server its key in `env`, not in `args`.
- **A proxy to `127.0.0.1` that leaves no trace looks local.** The page and "Files" refuse a request that carries a
  header proxies and tunnels add (`X-Forwarded-*`, `Forwarded`, `Via`, `X-Real-IP`, `True-Client-IP`, `CDN-Loop`,
  `Cf-*`, `Tailscale-*`), and a request from a browser page at another address (its `Origin` or `Referer`). A proxy that
  adds none of these and sets `Host: localhost` (plain `nginx proxy_pass http://localhost:8787` by default, `socat`,
  an SSH port forward) cannot be told apart from a program on the machine when the client is not a browser (an app,
  `curl`): such a client gets the operator's access. Do not point a proxy or tunnel at the page; use Tailscale or the
  Cloudflare provider above (`cloudflared` adds `Cf-*` headers, so a tunnel without a valid Access token is refused).
  `hub doctor` names proxy programs it sees connected to the page port, as a hint.
- **Page plugins and modules are not isolated.** Page code (`"page_access": "full"`, declared or not) can call any page
  endpoint, including installing a plugin and pressing "Allow and update" for an update that asks for more rights. Such
  consent is recorded in the journal (`plugins.consented`) and reported to you in Telegram when it is connected, so a
  forged one is visible afterwards; it is not prevented.
- **A catalog pins code; it does not vouch for it.** An entry is installed exactly at the commit its catalog chose, and an
  update that asks for more rights (a new process or command, page code, hooks or MCP servers of a skill set, new rights
  of a module) waits for your consent. A plugin of a Claude Code or Codex catalog (a skill set entry) is installed by the
  hub session or the agent developer without asking you — nothing of it runs until it is given to a session, and giving
  one that runs code needs your word; an entry such a catalog does not pin is pinned by the hub at the commit current at
  install. New code with the same rights — a new version of a page plugin, say — installs
  with a plain "Update". What protects you there is who maintains the catalog and whether they read the change; a
  third-party catalog is only as trustworthy as its author.
- **The text of a skill is an instruction to a session.** A plugin of text only is given to sessions without your yes,
  and sessions run with `bypassPermissions`: a skill that tells the model to run a command or send a file out is obeyed
  like a request. The agent developer reads a skill before giving it; that catches the plain cases, not every one. You
  see each grant in the session's feed and take it back with `hub tools revoke`.

## How to run it more safely

Pick the setup that matches what you can afford to lose.

- **Best:** a disposable virtual machine or a container with nothing valuable in it. Give it only the repositories and
  credentials the work needs, and throw it away when you are done. A container that shares the host network or your home
  folder is not isolation.
- **Minimum rights:** a separate OS user for the hub, narrow API keys, no cloud administrator credentials, MCP servers
  limited to what you use. Do not run the hub as `root`.
- **Network:** keep the page on `127.0.0.1`. If you need it elsewhere, use a private network such as Tailscale, never the
  public internet, a tunnel or a reverse proxy to `localhost` (the hub refuses those it can recognise, see above).
- **Backups:** keep backups of the repositories sessions work in and of the hub's state folder. A session in
  `bypassPermissions` can delete or overwrite files, and git history alone does not cover uncommitted work.
- **Review:** read plugins, skills and MCP servers before you install them, and read the approvals the hub asks you for
  instead of clicking through. Start sessions only in folders whose code you trust (their hooks and MCP servers run).
- **Channels:** bind Telegram to a group only if you trust each of its admins with the hub, and link the bus only to
  hubs whose owners you know.
- **Stay current:** keep updates on.

## Supported versions

Security fixes go into the latest release only. The hub updates itself from the repository, so staying current is the
default.

## Reporting a vulnerability

Do not open a public issue for a vulnerability.

Report it privately with GitHub's **private vulnerability reporting**: the "Report a vulnerability" button on the
repository's Security tab ([report a vulnerability](https://github.com/icons8/hub/security/advisories/new)).
Please include what you found, the steps to reproduce it, the version (the "About" page of the hub, or
`git rev-parse HEAD`), and what an attacker could do with it. If you have a fix, describe it; do not publish it
before we have talked.

What to expect: an acknowledgment within a few working days, an assessment, and a fix or a clear explanation. We credit
reporters in the release notes unless you prefer not to be named.

## In scope

- Anything that lets a party who is **not** the hub's operator run commands, read files or send messages through the hub:
  the web page and its API, "Files", the Telegram bot, the hub bus and its relay, the plugin and tool catalog, the
  update path, the `hub` command.
- Another website or another device reaching the page or "Files" in a way the checks above are meant to stop.
- Ways to make a session do more than the operator approved (bypassing the hub's approval gates), or to pass off a
  session's words as the operator's from outside the machine's user.
- Installing or running code from an update that the update path says it refuses (rewritten history, rollback, a moved
  tag).
- Secrets reaching the journal, logs, command lines or files readable by others in a form the mask is meant to catch.
- Leaking data the hub says it keeps local.

## Out of scope

- "A session in `bypassPermissions` mode can run commands": this is the documented design.
- Anything that needs a boundary listed under "What is NOT a security boundary" or "What is still open": a session or
  plugin acting as the same OS user, a process on the same machine using `127.0.0.1:8787`, changing `HUB_SESSION`, or
  writing to the state folder directly.
- A page plugin or an MCP server you installed doing what its manifest declares; a hook or an MCP server of a
  session's folder running as that folder declares it.
- An admin of the Telegram group you bound, or a hub you linked by the bus, acting with the rights this document gives
  them.
- Vulnerabilities in Claude Code, Telegram, Tailscale or other software the hub drives; report those to their authors.
- Attacks that need prior control of the operator's machine or account.
- Results of running the hub in a way this document advises against (public exposure, shared machines, shared accounts).
