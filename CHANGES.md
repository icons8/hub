# What's new in the hub

Newest first. Every hub shows this list: the "Updates" page and the message after an update take their items from here.
An item from someone else's PR ends with thanks: "— thanks: @login".
The "### For sessions" subsection under a date is what changed for the sessions themselves (commands, rules): the core
attaches to each session's next message only what it has not seen yet; the page does not show it.

## 2026-11-24
- A session whose conversation file is gone from disk (a cleanup of old transcripts in `~/.claude/projects`, a disk
  cleaner) no longer crashes in a loop. Before, the core raised it with `--resume`, Claude Code failed at once with "No
  conversation found", and so on at every start. Now it starts a new conversation with the same id, the session is told
  that the old one is gone and what it knew is in its files, and the spawn fact says `"conversation": "lost"`. A
  conversation found in any project folder is resumed as before; with no `projects` folder at all (another config
  folder, a stand-in claude) nothing counts as lost. Nothing to do: it arrives with the update, a running hub needs
  `hub restart` to apply it — thanks: @kalimullin
- A message you type in the page's field is in the feed the moment you press Enter, as a row of your own with "sending"
  over its head line (shown only if the core takes longer than a blink), and the core's row takes its place when the
  session has it. Before, your words showed only after the core had recorded them and the page had caught up: a
  second or three, longer under load, and the field looked as if nothing had happened. The row you see on Enter is
  drawn as the one that replaces it: the same place and size, with the quote, the picked cards and the pictures, in
  Classic and Clean; a picture keeps its image and does not blink. It is shown only where the core really takes the
  message at once: not for a command ("/name"), files without words, a busy, starting, sleeping, parked or compacting
  session, or one with an open card — those show their own lines ("Running", "In the queue") as before. A message the
  core refuses goes back into the field with its words. A voice message that is still being recognized keeps its
  own head line. Nothing to do: static files only, a page reload (⌘R / Ctrl+R) picks it up, no `hub restart`
  — thanks: @kalimullin
- Clean: three fixes to the heads. An executor raised by a specialist now wears the head of that specialist (the head of
  its role), not the head of the emoji its profile declares; an executor you gave an icon of its own keeps that icon. The
  Hub's name on a feed message shows what the Hub's row in the list shows: the head of the icon you picked for it, your
  own emoji, or the hub's icon when there are no pictures, instead of always the Hub's own head. And a message whose
  author the core had drawn with the theme's picture gets that picture back when the author no longer has a head
  (an emoji of your own), instead of keeping the old head. Nothing to do: static files only, a page reload (⌘R / Ctrl+R)
  picks it up, no `hub restart` — thanks: @kalimullin
- `hub send <session> … --fyi` (and `hub wait`) no longer say "it gets it with its next turn" / "waiting for a reply"
  after the core has refused the command: you see only the refusal and its reason, and the exit code is 1, as for an
  ordinary send. Nothing to do.
- A message from a session to an executor it raised itself that has since been removed is now refused with the
  reason ("devops has left (removed), and you raised it"). Before, it was "routed to whoever raised it", that is back
  to the sender, and the executor got nothing; the sleeping case has been refused this way already. Messages from the
  owner and from other sessions to a removed executor still go to whoever raised it, with its last result. Nothing to
  do: it arrives with the update, a running hub needs `hub restart` to apply it — thanks: @kalimullin
- That refusal now also says what to do next: `hub session revive <name>` brings the same executor back (the session
  that raised it may), `hub task run …` raises a new one. Before, it only said why. Nothing to do: it arrives with the
  update, a running hub needs `hub restart` to apply it — thanks: @kalimullin
- A session raised while a core module with `launch.env` was not answering (restarting with `hub restart mod:<name>`
  and slow to come up, paused, stopped, or its call failed) no longer goes without the module's variables silently.
  Before, its claude started without them and stayed so for hours, and nothing said it. Now the spawn fact names the
  modules (`"without"`), `hub status` shows "came up without the environment of module …" under the session (with
  `hub session reload <name>` to restart it at once), and after the module's next start a free session restarts with
  its environment (one with no task is parked and gets it on its next message; a busy one, or one with background
  work, waits until it is free). The short wait before a spawn stays as it was. Nothing to do: it arrives with the
  update, a running hub needs `hub restart` to apply it — thanks: @GreatMedivack
- A direction's document can take its text from a file or from stdin: `hub direction <d> doc create "<title>"
  --content-file <path>`, `… doc update <id> --content-file <path>`, or `--content -` with the text piped in. Before, the
  text went only as an argument, and a long document on Windows failed with "Argument list too long" (the command line
  there holds about 32K characters). An empty file or stdin is refused like an empty `--content`, and nothing is
  written. Nothing to do: it arrives with the update — thanks: @Eugenius-S
- The app: after the restart onto an update installed in the background, the page no longer says "Restarting for
  <version>…" (and offers "Restart" again) on a hub that already runs that version. Before, the line stayed until the
  update's next look at the server, which comes after the migration and the network, so the hub looked stuck and a
  second press restarted it again. Now the core, as it starts on the installed version, ends the wait at once; a
  restart of the core alone, still on the old version, keeps it. Nothing to do: it applies from the restart onto the
  version that carries it.
- One agent can have a CLAUDE.md size cap of its own: `"agents": {"profile_max_kb_for": {"<agent>": 400}}` in
  hub.json, `0` for no cap for that agent. Before, the cap (`agents.profile_max_kb`) was one for the whole hub: an agent
  whose pinned part is large kept asking to fold what it cannot fold, so the whole hub had to stay on a high cap and
  every other agent's file could grow with it. Now the shared cap stays as it is for everyone else, and an agent not in
  the list (or with a value that is not a number of 0 and up) gets the shared one. Nothing to do: add the key to hub.json
  if you need it; the core reads it at the next check, but a running hub
  needs `hub restart` to apply the update — thanks: @Eugenius-S
- An executor that asks another session something in the middle of its task (`hub send`) and ends the turn waiting now
  finishes that task on the answer: the turn that reads the answer continues the task, so its final report reaches
  whoever set it. Before, that turn belonged to no task, and the report went nowhere. This is common, not a rare case:
  on a week of one busy hub's journal it moved 115 turns of 48 executors to their tasks, and 87 reports reached whoever
  set them. The answer is the first letter from the asked session, however it arrives (its own message, its turn result
  the core passes on, an FYI); the next one is its own matter again, unless the executor says it still waits for that
  session (`hub waiting` naming it) after an "accepted, will send". When a session whose letter the turn took (the
  turn's own or one that came mid-turn) itself waits on the executor (`hub waiting`), the turn answers that session, as
  before, and the answers it read wait for the next letters. A question ends unanswered after a day, when its task
  closes, or when the executor leaves. Only executors: a specialist's letters are unchanged. Nothing to do: it arrives
  with the update, a running hub needs `hub restart` to apply it — thanks: @kalimullin
- Hub bus: a session that got another hub's question and passed it on to another session (`hub send <session> "…"` in
  its turn on those words) no longer leaves that session unable to answer in the thread. Before, the other session's
  `hub send liaison --to <hub> --thread <T>` was refused with `thread_closed` (only the session that leads the thread
  may write in it), and its answer went in a new thread, apart from the question. Now the core marks such a message: the
  session it went to may answer with `--thread <T>`, its answer makes it the thread's lead, and the other hub's reply
  comes back to it. The session that passed the question on keeps the thread until then, and may still write there
  after: the reply goes to whichever of them named the thread last. Without `--thread` the core never moves a thread
  another session leads (a session often writes to that hub about another matter); it continues only a thread nobody
  leads yet. A word to another session outside a turn on that hub's words, an FYI or a command passes nothing on. A
  `--thread` refusal now names the session that leads the thread ("pass your words to it"), and the words of another hub
  passed to a session show it the thread only if it may answer there. Nothing to do: it arrives with the update, a
  running hub needs `hub restart` to apply it.
- External hubs: what goes to the other hub is what you saw and wrote on the liaison's card. Before, when you answered
  its "Send to …?" card in words, the other hub could get the old draft: your words went with the core's head "Answer to
  question qN («…the old text…»)" (or "This is the user's answer … passed on by the hub" and the Telegram note when the
  Hub passed them on), or the liaison sent the session's first words as they were. And its message on your correction
  went as a new conversation, so the other hub's answer came to the liaison instead of the session that asked, and that
  session was told half an hour later that its request was not handled. Now your words by reference go without the
  core's lines (the same words the page shows you as your answer); after you answer the card in words (a correction, or
  "yes" typed), or close it and write within half an hour, the session's old words are refused (`owner_changed`) and the liaison sends the
  text as you approved it; and that message continues the session's conversation with the other hub, the answer comes
  to the session. A press of "Send" with no words of yours about that message works as before. Nothing to do: it
  arrives with the update, a running hub needs `hub restart` to apply it.
- External hubs: the liaison's own message on a session's words continues that session's conversation with the other
  hub also when the session wrote to it without naming the hub (`hub send liaison "…"`): the conversation it was in
  when it wrote, else the latest one that hub's words came to it in; a conversation the session named still wins, and
  words of two sessions in one turn still start a new one. Before, such a message started a new conversation, and the
  other hub's answer came to the liaison instead of the session. The liaison can no longer decline an answer in a
  conversation this hub started: the other hub used to get "Hub … declined the request" about our own words; now the
  liaison is told to close it or answer in it (`decline_ours`). And a conversation named by its first characters, or by
  the mark around the other hub's words, is refused with its full id named when exactly one fits. Nothing to do: it
  arrives with the update, a running hub needs `hub restart` to apply it.
- A download of an update (the code layer, the app, an environment, Claude Code, the parts the hub fetches) no longer
  ends with "0.0 MB are kept" on a link where something on the way (a scanner, a proxy that inspects downloads) holds
  the answer back until it has all of it, and the whole file takes longer than a minute: an attempt that sat silent
  and brought not one byte now asks for a quarter of the bytes it asked for (4 MB at most, 128 KB at least), and what
  comes is kept; a bigger window again only after one came whole. A failed update also says in its report what each
  attempt met: whether the server answered, how late, and where the silence was. The update to 1.10 itself is still
  downloaded by the old code of a 1.9.x hub, so this holds from the update after it. Nothing to do: it arrives with
  the update, a running hub needs `hub restart` to apply it.
- A session whose conversation was lost from disk right after Opus's safety filter moved it to another model no longer
  keeps the "back on the asked model" mark. Before, the new conversation (same session id) still carried it: the page
  showed the filter's row and the session was told that its stopped turn was cut from "the same conversation", though
  the whole conversation was gone. Now the mark goes with the lost conversation. Nothing to do: it arrives with the
  update, a running hub needs `hub restart` to apply it — thanks: @kalimullin.
- A bare `file:///abs/path` in a message, also one written in inline code, is a link into Files, the same as
  `[plan](file:///abs/path)` has been. Before, it stayed plain text: sessions often write the address as it is ("Saved:
  file:///…/plan.md"), and only the Markdown form was a link. The text stays as the message wrote it; a sentence dot, comma
  or colon after the address is not part of it, and `:12` after it is the line. An address that leads nowhere stays text,
  and the page still opens nothing on the computer itself. Nothing to do: static files only, a page reload
  (⌘R / Ctrl+R) picks it up, no `hub restart` — thanks: @kalimullin
- "Sign in to Claude" now finishes on a hub whose machine has no screen (a Linux server, a container) opened through
  `ssh -L 8787:127.0.0.1:8787 …`, as the README tells a server owner. Before, the band took Claude Code's `localhost` link,
  which after the sign-in leads to a port on the computer you opened the page on, not on the server, and it hid the field
  for the code: the sign-in never ended. Now such a hub shows the link Anthropic answers with a code and the field to paste
  it into. A Mac, Windows, and a Linux desktop (also a hub that runs as a `systemd --user` service without `DISPLAY`: the
  user's graphical login counts) keep the one-click sign-in. Nothing to do: it arrives with the update, `hub restart web`
  applies the server's half — thanks: @kalimullin
- A queued message above the field that is not the owner's own words now begins with who it is from: "Core: The hub was
  restarted (asked by: …)", "maps: …" for a session, "Module telegram: …", a Telegram member's name. Before, the core's
  restart note stood in the queue next to the owner's task in the same form and read as typed by the owner; the owner's
  own rows look as they did. Nothing to do: static files only, a page reload (⌘R / Ctrl+R) picks it up, no
  `hub restart` — thanks: @kalimullin
- The install on a Linux machine with no screen (a server, `install.sh --server`) no longer says that the shortcut is "in
  the applications menu" right above "This machine has no screen": there is no menu to look in. The shortcut file is still
  written, and a machine with a screen (and macOS, Windows) tells about it as before. Nothing to do: it shows at the next
  install — thanks: @kalimullin
- When the update and «Setup» download Claude Code at the same time, the second one now writes "waiting for the other
  install" to `logs/setup.log` instead of "FAILED busy": it is no failure, the first one finishes the job, and the line
  no longer sends support looking for a fault that is not there. A lock that fails for another reason is still logged as
  a failure. Nothing to do: it arrives with the update, the outcome of the install does not change — thanks: @kalimullin

## 2026-11-23
- A file that a session's schedule job sends (`hub send-file` in the job's command) is now also a row of the hub feed,
  signed "sent you by <session>". Before, it lay only in that session's own feed, so with the session asleep you saw
  nothing on the page. Nothing to do: a page reload (⌘R / Ctrl+R) picks up the page part, and `hub restart web`
  (the update does it itself) loads the server part — thanks: @elenalo91
- A long text pasted into the field and sent as a file shows in the feed as a card, as in the apps: its first words in up
  to two lines and its size, instead of a line "pasted-1.txt · 4 KB"; a click gets the file as before. Other files of the
  same message go in lines under the cards. Nothing to do: static files only, a page reload (⌘R / Ctrl+R) picks it up, no
  `hub restart` — thanks: @visualpharm
- A feed with many file paths in it no longer stalls the page: `/api/paths` ran `git worktree list` anew for every path
  that was not found in the session's folder (150 paths, 150 runs, an answer in 15–20 s on a big repo, and the browser's
  connections stayed busy, so the feed did not update). The list is now kept for 30 s, like the repo root and its files;
  a worktree made in the last 30 s is picked up by the next lookup after that. Needs `hub restart web` (the update does
  it itself); nothing else to do — thanks: @DeValar
- An executor's schedule job now wakes it after it has handed in its task and fallen asleep. A job it set for itself
  (`hub schedule add … --session <itself>`, a repeating one or one more than a day ahead) or one its raiser set for it
  continues its task at the set time, and the result goes to the raiser as a continuation, the same as a wake with
  `hub session wake <name> --about dN`. Before, the firing went to the raiser with "wake it", and the executor never
  woke. Places and the queue count as for that wake: with no free place it waits in the queue (a repeat firing adds
  only its check's news). A job set by another session still goes to the raiser. Needs `hub restart` — thanks:
  @GreatMedivack
- The core's "still connecting MCP" mark of a session's process now ends with that process also when it ends without
  an exit event: sleep, parking, a stop, `hub take`, a restart for tools or a rename, a kill of a hung launch. A process
  that went before its first turn kept the mark until the next launch, and `hub tools grant` answered "already has it"
  for a server that had failed in the session. Needs `hub restart` (the update does it itself); nothing else to do
- A session whose Claude Code is still connecting its MCP servers no longer looks like a free "idle" one. A new
  process (after a restart, a wake-up or `hub session reload`) reads no input until its servers are up, for as long as
  3 minutes; until now `hub status` and the page showed `idle` and a sent message just sat there. `hub status` now has
  a line under the session — `waiting for MCP: <servers> (N s left)` — and the page's list says "Waiting for MCP…",
  with the servers and the time Claude Code stops waiting in the tooltip. Only the display changed: delivery,
  waiting times and the state itself are as before. Plugin API 1.41: the session row in the snapshot has `mcp_wait`.
  Nothing to do: a page reload (⌘R / Ctrl+R) picks up the page part, and a running hub needs `hub restart` to apply the
  core part — thanks: @sanchopanda
- While you are away and write to the hub yourself (an away with deputies holds through your messages), the card the
  hub asks you in answer comes to you at once — on the page, in Telegram, with a push — instead of waiting for your
  return. Before, such a card (for example, "this area for your deputy — ok?") was held until `/back`, so an area could
  only be changed by typing it out in full. This applies to the hub's cards in a turn on your message of the last two
  hours; every other card of the away waits for you as before. Needs `hub restart`
- While you are away with deputies, the hub can pass on your answer to a card asked in your absence again. Your
  words from the page or Telegram do not end such an away, so a card asked without you never counted as seen, and
  `hub send <session> --for mN --card qN` was refused as "no open card asked before the owner's words" even hours
  later. A card now counts as seen from your first message after it was asked: that message answers none of them,
  your later ones answer the card the hub names. Words that name no card still close none of them until you are back.
  Needs `hub restart` (the update does it itself); nothing else to do
- Hub bus: when a session asks the liaison to ask another hub something and the liaison writes the message in its own
  words, that message now continues the session's thread with that hub, as a forwarded message already did. The
  request also counts as handled, so the session no longer gets "The liaison has not handled your request" half an
  hour after its words went out. When the liaison declines a session's request, the other hub is no longer sent
  "Hub X declined the request: <reason>". That hub asked nothing, and the reason was this hub's own business. The
  session that asked is told instead. In the liaison's own words, the core's labels (`[hub:mN …]`) and the fence lines
  around another hub's words are dropped before sending. A session's words sent by reference go out as written. Needs
  `hub restart` (the update does it itself); nothing else to do
- macOS: a sleeping Mac no longer starts work in its short wake-ups. Every ~15 minutes a sleeping Mac wakes for a few
  seconds without you (DarkWake), often with no network. A schedule job, a message or a session launch that started in
  such a window sent its request, the Mac slept again, and the turn hung for 3–4 hours and failed with "Request timed
  out". Now in these windows the hub only queues: messages and schedule jobs wait, sessions are not started, and job
  commands do not run. When you wake the Mac, each goes once. A turn the network cut is continued after the full wake
  too. The watchdog no longer counts sleep as waiting, so it no longer reports "not delivered" and wakes a session for a
  message held overnight. Linux and Windows: nothing changes. Needs `hub restart` (the update does it itself); nothing
  else to do
- Clean: new characters. The 24 session pictures are new clay heads, each on a plate of its own color (a light and a
  dark tint); the Hub has a head of its own in the list and on its feed byline. A specialist with no icon chosen by you
  gets the head of its role (a word of its name or of its profile's title: "designer", "analyst", "liaison"…), else one
  by its name as before; an icon you pick in the picker still comes first, an emoji of your own stays an emoji. A session
  that had a character keeps the same slot, so it changes its picture once. Reload the page (⌘R / Ctrl+R) — thanks:
  @kalimullin
- A card handed to a deputy (`hub question route`) is no longer shown as waiting for you: `hub show qN` says "state: open
  — routed to <name> (deputy), waits for their answer"; once they have answered it is no longer "open" — "state:
  answered by <name> (deputy) — close the card: hub decided --card qN …" — and `hub status` lists it as "at <name>
  (deputy)" or "answered by <name> (deputy), not waiting for the owner". A card that came back to you reads as before. Needs
  `hub restart` for the core part.

## 2026-11-22
- Away with deputies (`/away deputies`, `hub away --deputies`) now lasts until you say you are back — "I'm back" on
  the page, `/back`, `hub back` — or until its `--hours` run out. Your own messages from the page or Telegram no
  longer end it, so the deputies keep taking the questions of their areas while you write to the hub; urgent things
  still reach you in Telegram as in any away. A plain away without deputies is unchanged: your next message still ends
  it, so a forgotten `/away` does not hang for a day. It holds while a deputy you named is on the list, even if their
  channel is down for a while (their hub in quarantine, the session of their channel gone): their cards then wait for
  you. `hub away --status`, the line under `/away deputies` and the page's tooltip on the away strip say that it is
  held. Nothing to do: it arrives with the update, and a running
  hub needs `hub restart` to apply the core part.
- A deputy can act for you in their area: tell the hub «X acts for me on <area> while I'm away — everything there is
  theirs» and it marks them (`hub contacts deputy "X" "<area>" --all`). While you are away with the deputies, every
  question of that area goes to them — prod, money, merges, publishing, access, «only on my word», 🚨, their own
  request — and is not held for you. Never to any deputy: your personal data, your correspondence, a text in your name
  and what needs your own hands (Touch ID, a code from your phone, a password) — those wait for you. Their answer is
  their word, not yours: in your return summary under «Decided by your deputies», their own requests marked. The mark
  shows in `hub contacts deputies` and `hub away --status`; to take it off, tell the hub to set them again without it,
  or remove the deputy — the cards they hold come back to you. A deputy without the mark decides as before. Needs `hub
  restart`.
- Such a deputy's decision also opens what the hub would otherwise keep waiting for your word: a tool for a session
  (never one the catalog marks as holding your own data — your mail, chats, browser sign-ins: those still wait for
  you), an executor, a spec's acceptance, an agent's role. Your liaison's forms on messages to other hubs go to them
  too (never promises or deadlines in your name, nor your personal data or correspondence); the hub's own checks of
  whom to trust (a pair code, a link offer, an introduction) stay yours. Your summary says what each decision opened.
  It counts while you are away with that deputy and for a day; removing the mark or the deputy, or your return, ends
  it.
- The hub page says when a session is compacting its conversation, so the wait after a message or a wake-up no longer
  looks like a hang. While Claude Code (or Codex) shortens a long conversation — at its own threshold, after `/compact`,
  or when the hub compacts a session it is waking — the line above the message field reads "Compacting the
  conversation…" with a spinner, the session's row in the list and the header say "Compacting…" instead of "idle" or
  "working", and the session counts as working. A message you send meanwhile waits in the queue and goes right after,
  and the hub does not park the session in the middle of it. Plugin API 1.40: the session row in the snapshot has
  `compacting`. Nothing to do: a page reload (⌘R / Ctrl+R) picks up the page part, and a running hub needs
  `hub restart` for the core part.
- `/away` and `/away deputies` typed in the message field now answer where you typed them, on every screen and in Clean
  too: the line Telegram sends (away until when, what the deputies take, "held until /back") under the field — on a
  phone the old one-line hint was hidden, so it looked as if nothing had happened — and a repeat while already away says
  "Already away (since …), now until …" instead of the same answer. On a phone the header of a session carries an
  "Away" mark (the strip over the list is a screen away there). Nothing to do: a page reload (⌘R / Ctrl+R) picks up the
  page part; the answer's text comes from the page server, so it needs `hub restart web`.
### For sessions
- A deputy marked «(everything in it)» acts for the owner in their area: route them every card of it, the owner's own
  gates included (prod, money, merges, publishing, access, «only on my word»), and their own request with
  `hub question route qN <name> --own`. Never to any deputy: the owner's personal data, their correspondence (in the
  question, the options or `--note`), a text in the owner's name, what needs the owner's own hands. Their answer is the
  decision on that card for your rules and your repo's gates, never for writing in the owner's name. Record it first
  (`hub decided --card qN …`), then a gate of the core that needs the owner's word takes `--for qN` (`hub tools grant`,
  `hub task run/go`, `hub task spec accept`); a tool with the owner's own data still waits for them.

## 2026-11-21
- The hub page keeps what you were writing: the text in a session's message field is still there after the page
  reloads (the hub updated, the browser restarted the tab), and the "Reply" quote stays with its own thread — a look at
  another thread no longer drops it, and it survives a reload too. Kept in this browser for 14 days, removed the moment
  the message is sent; two open tabs do not overwrite each other, and a draft sent in one tab leaves the other. If a
  send got no answer and the text came back into the field, sending it again after a reload does not double the
  message. Attachments are not kept (the `[Image #N]` mark stays in the text), and a plugin's own field stores its
  draft itself, as before; the fields of plugins, boards and linked hubs are not touched by another tab's drafts. A
  "Reply" kept from before the reload to a session that is gone (closed, renamed) is dropped, and a very long quote is
  kept with its first 4000 characters. A page reload is enough, no `hub restart` — thanks: @visualpharm
- The hub page, voice messages: a voice message is addressed when you press "send" (the button, Space, the release of
  a hold, the stop on another session's page, the silence stop). Its "Reply" quote, the board cards picked and the
  question card the field is aimed at go with it, and so do the text and the files then in the field; the quote, the
  cards and the card's aim leave the field at once. What you set, type or attach while it is transcribed stays in the
  field for your next message: before, a reply or a card set meanwhile went with the message already spoken. If
  nothing is sent (cancelled, no speech, the transcription failed, a file of it failed to upload), the quote, the cards and
  the aim come back. The message is in the feed as soon as you press "send": "Recognizing…",
  then its words, then the hub's own row in the same place. Reload the page (⌘R / Ctrl+R). — thanks: @visualpharm
- The hub page: the search in the session list finds names by the same rule as the phone and Mac apps. Case,
  accents and the separators `-`, `_`, `.`, `/` and spaces do not matter ("hub dev" finds "hub-dev"); a query of one
  letter matches only the start of a name or of a word; "bru" typed on the Russian keys finds what "bru" finds, after
  the names that match as typed. Opening a session from the search clears it, so the whole list is back. The Russian
  keyboard layout comes from the language data through `/api/keyboards`, so it needs `hub restart web`; until then
  the search matches only what was typed — thanks: @visualpharm
- Accounts, the rest of the gateway flicker at the end of the week (after PR #601): when this computer's login is at
  its limit too, a try of the gateway's fresh token no longer tells the hub "limit economy is over", then "none free"
  and the pool card, and no longer adds a "new token on command" line every 30 minutes. Such a try keeps "all busy"
  and the economy mode until its limits come: with reserve "all busy" ends once, as before; at the limit, silently. At
  the reset that ends "all busy", the account it freed comes first, before another try of the gateway, when the
  gateway's last try was read. Needs `hub restart`. — thanks: @Enwimo
- A hub process that is already running keeps the texts it has read, even when an update moves them out of a catalog
  or a catalog file is caught half-written: a message no longer shows a raw key (such as `core.accounts.limit_pct`) in
  the minute before the restart. A changed text is still taken at once. Works from the next update on, once the hub
  runs this version (`hub restart`). — thanks: @Enwimo
- Windows: `hub run`, starting another hub instance from the page, the watchdog of an app update and the uninstall no
  longer open a console window on the desktop (the uninstall's only flashed). The hub starts them outside its job through
  WMI, and the new console's window was shown; it is now created hidden. The app's launcher carries its own copy of this
  start: it changes with the next app update. Needs `hub restart`. — thanks: @Enwimo
- An executor with a one-shot job addressed to it (`hub schedule add --at`, usually its own) that then reported is no longer put to sleep
  two minutes later when the job is due within a day: it stays awake and the job's message reaches it at the set time.
  Before, it fell asleep and the message went to whoever raised it with "wake it", so the window never started; a job
  further than a day ahead does not hold the place. — thanks: @GreatMedivack
- Accounts gateway, a pool of one account whose token comes from a command: when that account hits its limit, the
  requests already sent (a turn, a batch of subagents) no longer fail with "You have hit your … limit". The gateway holds
  each of them until the hub takes a fresh token (usually 1–3 seconds, at most 15) and sends it once more. When the
  hub takes none (it says the account is busy: the command gave no new token, or a fresh one was taken lately) or the
  fresh one is refused too, claude gets the refusal as before, at most a few seconds later. A pool of several
  accounts, an account with a plain token and an account waiting for its own reset work as before. The gateway's `status.json` counts
  limit refusals by the API's `overage-disabled-reason` (a spending cap and overage switched off look the same in
  claude) and the held requests; the reason also goes to the hub log. Needs `hub restart`. — thanks: @GreatMedivack
- `hub show qN` says where a question card stands: the second line is `state: open — waits for the owner's answer`,
  `state: answered` (the answer is the last line, as before) or `state: closed without an answer — …` with the reason: the
  owner closed the card, it was withdrawn (by whom and why), it went stale (no answer for over 24 h) or its session is
  gone; a card that left the list with no trace of how says only `state: closed — no longer waits for the owner`; the last line repeats the state of a card with no answer. A card the owner closed with the × used to end in "no
  answer yet", like an open one. Works at once, no restart.
- Sessions check what they hand you as waiting: before an answer ends with "Waits for you", each card in it is read with
  `hub show qN` — another session's card too — and named only while it says `state: open`; an answered or closed card
  and one not checked in that turn are left out. Before, a card you had already answered could stay in every answer
  for hours, carried over from the session's earlier answers or from its summary after compaction. Sessions take it
  with the hub's restart (`hub restart`).
- Windows: a text that starts with "/" reaches the hub as typed when a session sends it from its Bash. `hub slash
  "/ship maps-1"`, `hub send <session> "/…"`, the task of `hub session new … --cwd DIR` and `claude -p "/…"` used to
  arrive as "C:/Program Files/Git/ship maps-1": Git Bash took the text for a path. Now only a real path (its first
  folder exists, as `/tmp/…` or `/c/…`) is converted, as before. Needs `hub restart`. — thanks: @Enwimo
- An update no longer rolls back a hub that is up but still reading its journal. After a reboot or on a big journal the
  page needs minutes to read it, and the update's check (3 minutes from the start) did not see "the core came up" until
  then: the new release was taken back, marked as not coming up and not offered again. Now `/api/about` tells when the
  core started from the end of the journal at once, so the check passes while the page reads, also for the update that
  brings this release; a page whose journal reading has failed still tells no start. Needs `hub restart`.

## 2026-11-20
- A message in a session's feed has "Copy" beside "Reply": it puts the whole message on the clipboard, and the button
  says "Copied" for a moment (in the Clean theme it shows a tick). A file a session sent you and your own message with
  only attachments get "Reply" too: the quote is the file's caption or its name, and a press on that quote leads back
  to the file. Reload the page (⌘R / Ctrl+R). — thanks: @visualpharm
- A triple click on a message selects the whole message, not one paragraph, and "Copy" on the bar over the selection
  keeps its paragraphs. A double click selects a word without the bar popping up; dragging on from it into a phrase
  brings the bar. The bar stands just above the first selected line, inside the feed column, and never covers the
  selected words: at the top of the feed, under the head, it goes just under the selection. A plugin or a module that
  shows messages of its own gets the same once it marks a message's text (Plugin API 1.39, `data-hub-message`): a
  triple click selects all of it, and a selected piece gets the bar with "Copy". Reload the page (⌘R / Ctrl+R). — thanks: @visualpharm
- A session's message taller than the feed has a second "Reply" under it, at the bottom right, besides the top one, so
  after reading it to the end there is no scrolling back up to reply (the same arrow in Clean, the same quiet link in
  Classic). Whether it is taller is measured after it is drawn; a short message and one folded to two lines keep a single
  button. Nothing to do; reload the page (⌘R / Ctrl+R), no restart — thanks: @x6y9kax
- Reactions: the 👍 the hub puts under your message when a session answered it now leads to that answer. Press it and
  the feed scrolls to the session's reply and highlights it (older pages load if needed). In Classic, "Reaction" next to
  it changes the reaction; in Clean your own message carries the badge alone, with no "Reaction" control — a reaction
  taken off your own message cannot be put back in Clean (in Classic it can). While the page looks for the reply further
  back it says so under your message, and when the reply is not in the feed it says that for a few seconds instead of
  staying silent; a picker left open on the waiting 👀 closes when the link arrives; the 👍 is a finger-sized target with
  a spoken label. Clients read where it leads from the snapshot's `reactions.links` (API 1.39). Works with the
  "Reactions" module on; for answers after the update. Needs `hub restart`. — thanks: @visualpharm
- Deputies: in the contacts book, say what a person decides for you while you are away. Tell the hub "Masha decides
  the map tiles frontend for me", or "set Masha up as a deputy" and answer its one form (her area, what she does not
  decide); in a terminal, `hub contacts deputy "Masha" "<area>"`. `hub contacts` and Settings → External hubs show the
  area. Go away with the flag — `/away deputies` (or `/away deputies Masha`, `hub away --deputies`): a card whose every
  question lies in one deputy's area goes to her hub (or to the session that holds her Teams), saying who asks, on
  what task and why her; her answer comes back to the session that asked. Her word settles questions of her area only:
  never money, prod, publishing, a message in your name, the irreversible or access, and no gate of the hub passes
  with it. Your return summary has "Decided by your deputies" and the cards still at them; `/away` without the flag is
  as before. Needs `hub restart`.
- An update (or Claude Code, or an app part) that downloads over a poor link no longer stops at the same place for
  good. The download kept only whole 1 MiB blocks: a link that went silent in the middle of a block threw away what had
  come of it, so a link that gave less than a block each time never moved past "13.6 MB kept". Every byte that comes is
  now written at once; a try that brought bytes does not use up the run's four tries (a run is at most 16 tries); and
  a server or a proxy that answers the continuing request with the whole file is read past what is already held,
  instead of the held part being thrown away. Nothing to do: it arrives with the update. A hub already stuck on a
  broken download is on the old code until the layer arrives: `hub update` continues the same file.
- The liaison's refusal now reaches the session that asked. Before, when a session sent it words by a file or in any
  way the liaison could not carry ("send them the text from the file …": it cannot read files) and the liaison
  refused with nothing sent, its reason stayed in its own result: the session never saw it and took the letter for
  sent. Now, when the liaison's turn on a session's message did not carry its words out (nothing went to the bus
  for it), its result comes to that session as a message, once per turn; when nothing at all was sent, it is not
  shown to the owner a second time: the session tells them itself. A turn that sent or passed the words, or that
  closed an item of another hub or answered a person's message, is the same as before. Needs `hub restart` —
  thanks: @GreatMedivack
- Settings → Secrets: the buttons of a secret's card ("Copy password", "Copy login", "Change", "Remove"), the form's
  "Save" and "Cancel", and the button under a warning of the vault stood flush against each other, in every theme. They
  borrowed the class of the page-key row, whose rule left with that setting when the page password came. They now have
  the rule of their own, with the same 8 px gap as the other rows of buttons. Nothing to do: it arrives with the update
  (no restart: the page's styles are served from disk).
- An answer to a card with one question in the feed names the options not chosen along with the one chosen: "Chose
  Northern road; not chosen: Southern road · Coast road". Only when the page knows the picks: a card with several
  questions, own words typed in the card, or a pick with a comment that another page sent stay "Answer: …"; the
  Telegram message itself is unchanged; a tap the core refused does not color an answer given later elsewhere. Reload the page (⌘R / Ctrl+R).
  — thanks: @visualpharm
- The daily stops audit of the agents (`agent-dev-stops`) no longer shows red right after a hub start. A journal
  rotation removes `state.json` until hubd saves it again, and a check that fired in that gap failed with "no hub
  state"; it now takes the state from the journal then. A hub with `state.json` in place reads as before. Nothing to do:
  it arrives with the update. — thanks: @ManyLattice
- The page draws plugins sooner after it opens: it asks for every plugin's main file at once instead of one after
  another (the last of twenty plugins was fetched only after the nineteen before it had loaded and run); the plugins
  still run and register one after another, in the same order as before. A core module's page plugin can list its stylesheets in `module.json`, `"css": ["web/look.css"]` (Module API
  2.11, as the plugins' `css`): the page puts them into its `<head>`, so what the plugin draws is styled from the first
  frame, and the stylesheet can keep the place of what comes later. A look the person switched off stays off from the
  first frame of the next load: the plugin turns its listed sheet off with `hub.addStyle(file, { on: false })` (Plugin
  API 1.39); a plugin that stops asking gets its sheet back; an `async register()` that fails is reported as a failed
  load, as a sync one. The fold of a long message is measured against the feed
  as it stays once the field's task strip is in (a message just over the fold's bar came whole when the plugins
  registered before the page drew again). Needs `hub restart`. — thanks: @visualpharm
- The page: one message waiting over the input field is headed "In the queue", no "· 1"; the count stays from two
  messages on, and the folded row on a phone reads the same way. — thanks: @visualpharm
- Windows with a Russian (or any non-UTF-8) console: `hub on` no longer prints a `UnicodeDecodeError` traceback
  before it starts the hub. The scheduler's own message was read as UTF-8; it is now read in the console's code page,
  and if the start fails, the error shown is readable. `hub app undo` reads it the same way. The hub itself started
  before, so nothing changes in how it runs. Nothing to do: it arrives with the update. — thanks: @summers-julia
- The hub page: the screen recording's note for the session is now hidden also when the app wrote it without the length
  ("Screencast: a recording of my screen … — screencast-<id>.mp4"); the bubble showed it whole. When you wrote words
  before the note on the same line, the " — " before it goes with it: "look at the button", not "look at the button —".
  A note with the length is hidden whole even when its text has a colon of its own ("Screencast 0:04: a recording of
  my screen (note: voice) — …"); without the length the label is one word, so "look Screencast: a recording — …" keeps
  the word "look" and stays whole. As in the phone and Mac apps. A page reload is enough, no `hub restart`. — thanks:
  @visualpharm
- "Waits for you": a result that reports something handed or returned to another session - "that is item 14 of the
  session maps; she asked me to close it, but only she can, so I returned it to her" - no longer becomes an owner to-do
  item such as "close item 14". The watcher's question now gets the names of the hub's sessions the result mentions, as
  it already gets the open cards, so "she" after a session's name is that session. A result that also asks the owner
  ("handed the layout back to maps; from you I need the bank sign-in") still does. Takes effect after `hub restart`. — thanks: @visualpharm
- Clients tell a hub with a Claude account pool by `accounts` in the snapshot's `features` (Plugin API 1.39): it and
  `accounts.use` are listed only on a hub with the built-in pool or a login module for session sign-in. A hub without
  a pool (installed without the built-in accounts, no login module) no longer lists `accounts.use`, so an app does not
  offer to choose an account there. The page does not change: it already shows no accounts where the snapshot's
  `accounts` is null. Needs `hub restart`; reload the page (⌘R / Ctrl+R). — thanks: @visualpharm
- The times the page draws drop the leading zero of a 12-hour clock: "1:09 AM" instead of "01:09 AM" (English, Arabic,
  Hindi, Korean); 24-hour languages keep "01:09". The same in the feed's times, the Clean theme, the settings lists
  and the Usage and Flow views; a time written inside a line of text the core puts into the feed stays as it was.
  Plugin API 1.39: `hub.clock(ts, date?)` gives a plugin the same clock. Reload the page (⌘R / Ctrl+R) after the
  update; no restart needed — thanks: @visualpharm
- The open list of a drop-down in the dark theme is readable again: on "Flow" the period list (Now, 1 h, 6 h, Day) and the
  other lists of the page (the speed, sorting in "Plugins", the "Settings" choices) were light-grey text on the
  browser's white, about 1.2:1. An item now has the theme's page colour behind it and its text colour, 13:1 and more
  in Classic and Clean, light and dark; a theme of the catalog gives the list its own page and text colours. A page reload
  is enough, no restart.
- Clean on a Mac, an iPhone and an iPad: the emoji in a reaction plate stands in its optical center (Apple's emoji font drew
  it about 2 px left of the middle and 1 px high); other systems' emoji fonts stay as drawn, and a theme's own adjustment of
  an emoji (`[data-emoji] { transform }`) replaces the offset. Reload the page (⌘R / Ctrl+R) — thanks: @visualpharm
- A plugin's view on the page can search with the page's own search field instead of drawing a second one: while such
  a view is open, the field above the session list shows the view's hint (for example "Find media") and searches
  inside the view; Esc or × clears it, Cmd+K focuses it. The session list is not filtered meanwhile, and leaving the
  view gives the field back to the session search with its text. Views without search are unchanged. For plugins:
  Plugin API 1.39, `search: { placeholder?, query(text) }` in `addView`, `hub.search({ placeholder?, text? })`, and
  `hub.searchField(...)` — the same search box for the view's own place on a narrow screen;
  `hub.capabilities.viewSearch === 1`. Reload the page (⌘R / Ctrl+R) after the update; no restart needed. — thanks:
  @visualpharm
- Telegram: a voice message whose file did not download because the network to Telegram blinked (DNS, a timeout,
  429/5xx) is no longer lost with "Could not make out the voice message". The bot keeps it (a bot restart keeps it too),
  says once that it waits for the network and downloads it again on its own for up to about 15 minutes (after 10 s,
  30 s, 1, 2 and 5 min), then sends the text as usual. If the network is still down after that, one line asks to send
  it again. Other failures (a file too big) are reported at once, as before. Nothing to do, `hub restart telegram`
  after the update — thanks: @Eugenius-S
- The limit economy card and the pool limit cards say which window's reset the date is: when the date is not the reset of the
  window named in the reason (the week holds a login in economy after its 5 h window has reset, or another pool account
  frees first), the card reads "until 20.11 03:00 (reset of the week window)" instead of a bare "reset" next to "window 5 h".
  Only the wording changes: the mode, when it starts and ends and the queue stay as they were. Nothing to do — thanks:
  @ManyLattice
- Telegram: when you answer the card "Spec dN" with words instead of "Accept" or "Drop" (a reply to the card or to its
  file, say "all yes, except …"), the words go to the spec's executor (a reply to the file used to go to the hub), and
  now whoever raised it, or the hub, can accept or drop that spec by them (`hub task spec accept|drop dN --for mN`). Before, this was refused as "the owner wrote it to
  the executor, not to you", and you were asked again. Only that spec counts your reply; it is no word for any other
  task or command, and words to the executor without replying to the card count as before. Needs `hub restart` —
  thanks: @GreatMedivack
- A broken character can no longer become a session icon: a lone surrogate (a byte that was not UTF-8 in the typed icon)
  reached the hub as the replacement character (U+FFFD), which passed as a sign, so the session got a broken badge. The character
  and lone surrogates are now refused with the same "an icon is one emoji" answer. Nothing to do: it arrives with the update.
- Messenger keys in the web page's conversation, as in Telegram Desktop and Slack: in the message field, while it is
  empty, ⌘↑ (Ctrl+↑ off a Mac) picks the newest feed row to reply to, more presses go to older rows, ⌘↓ back to newer
  ones and past the newest drops the reply; with the focus on the feed the keys scroll it as before. Esc with a reply
  quote over the field drops the quote; before, the same Esc also stopped the session's running turn; Esc in the session
  search only clears the search, and Esc that closes the panel by a selection in the feed now does only that. A letter
  typed while no field has the focus goes into the conversation's field. Reload the page (⌘R / Ctrl+R) after the update;
  no restart needed. — thanks: @visualpharm
- SECURITY.md now says what a hub on Codex sends to OpenAI and how its sessions run (no approvals and no sandbox in
  `bypassPermissions`, and how the other modes map to Codex's policy and sandbox). Documentation only; no restart needed.
- Windows: installing or updating a plugin that has a running process no longer fails with "folder is busy" (WinError 32). The hub switches the plugin off, restarts its part so the process lets go of the folder, swaps the folder and brings the plugin back, also when the swap fails. A plugin you had turned off stays off. Nothing to enable; it works on the next `hub market install` / `update`. — thanks: @Eugenius-S
- Windows: the count of your own edits of the core (the "Updates" band) no longer crashes on a file name with a non-ASCII letter (git output is read as UTF-8, not in the machine code page). — thanks: @Eugenius-S

### For sessions
- While the owner is away with the deputies, the core lists them with your card: a card whose every question lies in
  one deputy's area — `hub question route qN <name>`; their answer comes to you on qN, their word, not the owner's.
  It settles the question — act and close the card: `hub decided --card qN "<what> · <name>, deputy · rollback"`;
  not theirs — `hub question route qN --owner`.
- After `hub send liaison …` the liaison may answer you with a message "turn result on your mN": that is a refusal or
  a note, nothing went out to the other hub. Do not take the letter for sent; put the words into the message
  itself, not a path to a file: the liaison cannot read files.
- The hub's feed: the core's lines on a task ("STOPPED d8: b — the turn broke off…", "TIMEOUT d7: a has worked…",
  "REPLIED d9: a replied to the owner on m5: …") read as words in the hub's language, without the tag and the task
  and message numbers: those are for the session, which reads the same lines as before, in `hub show` too. In every
  language the hub speaks; the quoted reply stays as it is. "Detailed" and "Brief" alike. The feed's past gets the
  new lines as the archives are parsed again in the background (every archive once, at low priority — hours on a big
  hub; until then the page cuts the tag of an old line itself, as before). Needs `hub restart`. — thanks: @visualpharm

## 2026-11-19
- Tailscale is off until you turn it on: a new hub no longer opens its page to your other devices just because the
  computer is already in a tailnet. To turn it on: Settings → Modules → Tailscale (a switch on the card, it takes effect
  within seconds), or `"tailnet": {"enabled": true}` in hub.json. A hub where Tailscale worked before this update keeps
  it: the update writes `"enabled": true` into its hub.json once, so a phone does not lose the page (a computer away
  from its tailnet at the update is decided the first time Tailscale connects); a hub where it did not work stays off
  and does not turn it on later by itself, a rollback and a new update included. While it is off or not working, the
  Modules card and `hub status` say why and how to turn it on. If the hub's Tailscale part stops, the page and "Files"
  stop letting the tailnet in within 8 minutes instead of trusting its last addresses for good, and the card and
  `hub status` say so.
- Telegram: once the bot has an owner, `/start` of an admin in another group gives nobody a code: a card comes to your
  chat — who asks, which group, who would get your rights there — with "Move the bot there" and "Keep it here", and
  only your press (in a bound group, its admin's) moves the bot. `hub-telegram pair <code> --yes` runs only in your
  terminal (over ssh — `ssh -t`), not from a pipe, a hub session or a schedule job. Before the bot has an owner,
  strangers' `/start` no longer hold all five pairing places for ten minutes: a new request takes the oldest place.
  Needs `hub restart telegram`.
- Modules no longer see how you signed in to the page: a request the page passes to a module's endpoint or view goes
  without the `Authorization` header, the Cloudflare Access token and service-token headers (`Cf-Access-*`) and the
  sign-in cookies (the hubs' `hub_<…>` and Access's `CF_<…>`, such as `CF_Authorization`). Before, a module got them
  and could open the page as you. A module's answer can no longer clear or plant these cookies or ask your browser for
  a password (its `Set-Cookie` with such a name, `Clear-Site-Data` and `WWW-Authenticate` are dropped). Other cookies
  and headers go through as before; a module that checked its own `Authorization` header behind the page needs another
  header name. Needs `hub restart web`.
- Schedule: a command job (`hub schedule add --command`) that a session set now runs as that session, not as your
  terminal: its `hub` commands pass only the gates the session passes. Before, a session could set a job whose command
  approved its own risky tool, turned the hub's password off or bound the Telegram bot, and the run did it in your
  name. Jobs you set yourself (terminal, page, Telegram) run as before; `hub notify` from a job's own command still
  reaches you (milestones). A session's job that needed your rights now finds the core's refusal in its log. Needs
  `hub restart`.
- A risky tool (a server with your data, money or prod, a plugin that runs code) goes to a session only on your words
  that name it — now for the hub too. `hub tools grant <session> <tool> --for mN|qN` counts when your message to the
  hub, or the question of the card you answered, names the tool (its id or its plugin's name); before, the hub could
  cite any message of yours from the last day, so "show me the status" was a yes to the browser with your sign-ins.
  Otherwise you get the core's card for that tool, with what it can do, as from any other session. The same holds for
  new code in the hub: the hub's `hub module add|dev|trust|model`, `hub module update --accept`, `hub marketplace
  install` of a plugin or a module and `hub market install` with `--for` need your words, or the card you answered,
  to name the module or the plugin: a new source by its address (`owner/repo`), an entry of a catalog that is not an
  official one as `<entry>@<catalog>`, an installed module by its name. Removing and taking rights off take any word
  of yours, as before.
- The hub page: HTML inside a message can no longer borrow the page's own classes and markers. A `class` or `data-*`
  attribute written in a reply is dropped, so a message cannot draw a fake full-screen image viewer over the hub or
  hook the page's clocks; only a code block's `language-…` stays (highlighting, diagrams). A diagram's labels lose
  their `data-*` too, and the page's clocks and the feed's scroll position no longer pick up anything inside a
  message (a label that said "now" every second). Reload the page (⌘R / Ctrl+R).
- Files: the hidden folders (`.git`, `node_modules`, `__pycache__`, `.DS_Store`) stay hidden in any case: on a Mac a
  request for `.GIT` opened the project's `.git` folder. A `.git` that is a link to a folder kept under another name,
  and another spelling an old Mac disk opens as `.git` (with an invisible character inside), are hidden as well.
  Needs `hub restart`.
- Page: the browser-tab icon reads at small sizes. 32 px is its own drawing (12 rays), 16 px is the same drawing halved,
  48 px is the large mark; the red dot while a question waits is redrawn for each size, and a browser that asks for
  `/favicon.ico` gets the same tab icon. The Files page has it too. Reload the page (⌘R / Ctrl+R) —
  thanks: @kalimullin (icons8/hub PR #1).
- Page: the hub's icon beside its name (session list, feed, "stopped" row) is the same new drawing as the tab, not the old
  light plate with the sun. Reload the page (⌘R / Ctrl+R). — thanks: @kalimullin (icons8/hub PR #2)
- Clean: sessions have characters. A session whose icon is one of 24 emoji shows its character (24 pictures,
  AI-generated, Apache-2.0) in the list, in the feed and in the icon picker instead of the emoji; a session with no icon
  gets one by its name, always the same (an executor takes the character of whoever raised it). An emoji of your own
  (📦, 🦊) stays an emoji, and the picker keeps its own-emoji field. Settings → Look → "Chat avatars" off keeps the list
  to names. The pictures are plain png files that the page server of the previous release serves too, so nothing waits
  for a restart; if they do not load, the emoji or the first letter stays. Reload the page (⌘R / Ctrl+R). — thanks:
  @kalimullin
- Plugins: a skill or a command that Claude Code runs a command from — `hooks` in its frontmatter, a "!`command`" line
  or a "```!" block — now counts as running code, like `hooks/` and `bin/`. Such a plugin is no longer "text only":
  a session gets it on your word, its card names each command, and an update that adds or changes one asks you first
  (a plugin with such commands installed from a catalog before asks once, on its next update, naming them). Among the
  catalogs this moves `commit-commands` of Claude Code's official catalog (it runs `git status` and `git diff`); a
  session that already has it keeps it.
- `hub tools import mcp <name>` from the hub no longer writes your server as riskless: without `--risk` it gets
  `owner-data`, and what the settings already have for it stays — its risk (the hub only adds to it), `only`,
  `exclusive`, `as`, `with`; `hub tools import skill` from the hub keeps the skill's risk too. Your own import from a
  terminal writes the entry as you give it, as before.
- A server's install line (`--server`) gives the hub's user only the SSH keys of the one who ran it: the user `sudo`
  came from, else root (a cloud's user data: root's keys, or the key of the cloud's default user that root's line
  sends you to). Before, it took the keys of every user of the machine. Another user's keys go too with
  `--keys-from <user>`, on a run again as well (only the keys the file lacks). A run again looks into the hub's home and writes its `~/.ssh` as the hub's user, never as
  root, and a link there is not followed. A server set up before keeps its `~hub/.ssh/authorized_keys` as it is: on a
  server shared with other people, look into it and remove the keys that are not yours.
- A command (`hub send`, `hub session wake`, `hub task new`, `hub decided`…) or a hook whose text carried a byte that is
  not UTF-8 failed with a Python traceback (UnicodeEncodeError) and the message or the turn's fact was lost; it happened
  when a shell script under a locale the system lacks glued the first byte of "—" to a `$VAR` before it. Such a byte
  now becomes "�" when the fact is written, and the rest of the words arrive; the same for the text these commands
  read from stdin (`-`) under a UTF-8 locale. Paths in arguments stay as they are. No `hub restart` needed. — thanks: @visualpharm
- SECURITY.md and the README's Security part now say what the hub reaches and trusts by itself: Tailscale access to
  the page and "Files" (how to turn it on and off), the Telegram group and the chats with people, the hub bus and its
  relay, the agents' Chrome port on `127.0.0.1`, what a hub from the one-line installer trusts in its pages, the code
  of a session's folder (its hooks and MCP servers run), the HTML reports of "Files", the vault's "Copy", and the
  plugin catalogs the hub reads from GitHub. Nothing changes in how the hub works.
- Bus relay (`relay/server.py`, for whoever runs one): connections at once are capped — 1024 for the whole relay (never
  fewer than two for every long poll, `2 × HUB_RELAY_POLLS`) and 64 from one address; over the cap a connection is
  refused at once with 503 `connections` / `connections_addr`, and hubs try again later. Reading one request has 30
  seconds in all, so a client that sends a byte at a time no longer holds a place. Behind a proxy with
  `HUB_RELAY_TRUST_PROXY=1` the address cap counts the requests under way of the address the proxy names. Takes effect
  when the relay is redeployed; hubs need nothing.
- The agents' stops audit (`stops-audit.py --extract`) no longer hangs forever: the model call for each chunk ends at its
  deadline (15 minutes) together with everything it started, instead of waiting for pipes a grandchild of `claude` still
  holds. A chunk that missed the deadline goes to "not sorted out" with "no answer in 900 s", and the next `--extract`
  repeats only it. Nothing to enable — thanks: @Scremliss
- The hub has a new mark, the orange sun of dots: the browser tab (and its red-dot variant while a question waits for
  you), the app installed from the page (PWA, the home screen icon of a phone), the README and, in the open release,
  the Mac launcher `Hub by Icons8.app` (it takes the new icon when the install line runs again). The README has new
  screenshots: the Clean look next to an iPhone, the Telegram bot on an iPhone, all at @2x. Reload the page (⌘R /
  Ctrl+R); an app installed from the page takes the new icon when the browser refreshes it — thanks: @kalimullin
  (icons8/hub PR #1).
- Settings → Plugins: plugins of the Claude Code and Codex catalogs are no longer in the shared list. What the hub
  installed from them has a section of its own, "Claude and Codex plugins": for each — its catalog, what is inside
  (skills, commands, MCP servers), which sessions have it, a warning when a server's settings carry a placeholder instead
  of sign-in data (it will not connect), and the buttons "Give to a session…" and "Remove". The hub still installs them
  when you ask it and gives each to the sessions that need it. The other catalogs and the catalogs block at the bottom
  stay as they were.
- Builds with their own plugin catalog (Embed API 1.2): `modules/private.json` of a build's embed may name its
  catalog with `"catalog": {"name": …, "ref": …}`. The hub then connects two official catalogs by itself — the open
  one as `agents-hub` and the company's under its own name, from its branch — and each revokes only what was installed
  from it. A hub that had the company's catalog as `agents-hub` moves to the pair by itself at its first catalog pass:
  every installed plugin keeps its key, settings and on/off switch; it just belongs to the catalog that lists it now.
  See EXTENDING.md, "A build's own plugin catalog".
- `hub app src` and `hub app apply` on a build made of the open core and a company's embeds: the working copy is a clone
  of the open core at the release's core commit, and none of its commits carries the embeds — a pull request from it
  goes to a public repository. The build of your edits takes the embeds from the release you run. A working copy made
  earlier from a tree with the embeds is set aside as `own/core.before-<version>` (its origin removed) with your edits
  in it, and a new one is made; the Hub Developer moves the edits over as a patch.
- Publishing the open core and an embed: `tools/publish_pair.py <core repository> --embed <embed repository>` publishes
  one commit as two snapshots — the open core (everything but `embed/`) and the embed's folder into its own closed
  repository, the embed first, all checks of both before the first push. A repository whose tree did not change gets
  no commit. `--tag vX.Y.Z` / `--release` tags both under one number newer than the last release of either; a fix of
  the embed alone is a release of the embed alone. The embed's address is never an open repository.
- Page, Clean: the feed search on a computer is the same field as on a phone and in the left panel — a gray 36 px
  field with the magnifier inside on the left, the "1 of 3" count, the arrows and the close × inside on the right, no
  border of its own. On a phone the up arrow showed as a stray "↑" outside the field and "Cancel" was cut off at the
  screen's edge; both are fixed. Nothing to do to update — thanks: @visualpharm.
- Builds with an embed (Embed API 1.2): an embed may carry files under `deploy/` — its company's own Mac launcher
  signed with its Developer ID, for one — and a team build lays them at the same path of its layer, in place of the
  open flavor's launcher. See EXTENDING.md, "Files a build lays in place".
- Telegram group: `/here <session>` on a session that is neither an agent nor a direction (an executor, a plain
  session) no longer refuses — it offers "Make an agent", "Make a direction" and "Cancel"; an admin's press makes it
  one, as `hub session role` and `hub direction new` do (a running turn goes on), and binds the topic at once: "`maps`
  — agent, the topic is bound". An agent or a direction is bound at once, as before. `/here` alone says what the topic
  is bound to and who is who: each session's role and its topic as a link. Words in the group that begin with a
  mention of a person (`@name …`) are for that person: the hub reads them with its next turn, but they start none —
  no "typing", no "⏳". The late line reads "⏳ The hub is still busy: <what it does>" (with "your message is in the
  queue" while it waits; a session that runs no turn — "⏳ No answer from `maps` yet: …") instead of "The reply is
  delayed: hub — …". Needs `hub restart telegram`.
- A hub on Codex: `hub session model <session> <id>` and `hub task run … --model <id>` take the bare id of a model of
  the hub's Codex (`gpt-…` as the page's model menu lists it) and turn it into `codex/<id>`; before, they were refused
  with a hint that named only Claude's models. An id the hub's Codex does not list is refused with the form and a few
  real ids, and so is a Claude name on a hub entirely on Codex (it used to run the hub's model silently). `--work
  risky` on Codex now runs on the deep model of the list (Astra), as risky work runs on opus with Claude; before, it
  got Codex's default workhorse model with high effort. "Settings → Executors" still decide when they name a model.
- A session's own helper agents (Claude Code's Agent tool, Codex subagents) are counted in its row of the session list
  with their model and how long they run: "3 helpers · gpt-6.2-astra · 25 min" instead of "in background 3: /root/…",
  in Classic and in Clean's detailed list; the tooltip names each helper with its model and time, the rows under
  Classic's session header call them helpers too, and `hub show` names the model. The model is the one the call named:
  Codex always names it, Claude Code only when the session chose one (otherwise the row shows the count alone). On the
  row of an idle hub the helpers take the place of "main session". Reload the page (⌘R / Ctrl+R); the model appears
  after `hub restart`.
- Sessions hand work someone waits for (a task, an investigation, a review) to the hub's executors, not to their own
  helper agents: those are for a search, reading or a parallel check inside the turn. Before, a session could run such
  work as Codex subagents or background Agent calls, which the session list does not show, and the work looked stopped.
- The open build no longer carries the team build's delivery server (`deliver/`): the open hub is given out by pages and
  releases, and nothing in it used the folder. A hub from git is unchanged.
- A new hub greets you when you first open its page, in your browser's language (the hub's language in Settings →
  General still comes first): on a machine set to English a Russian browser got an English hello over a Russian page.
  The hub check speaks the page's language too: a check made in another language runs again by itself, and the red
  band of the hub check no longer shows rows in a language you do not read. A check made before you signed in to
  Claude (the first sign-in timed out, the code went in later) no longer stays a red band until "Retry" and a reload:
  the page drops it and checks again within seconds; a check that found trouble before a restart runs again after it.
  Needs `hub restart`.
- A hub switched to Codex on the band: the hub restarts onto Codex only once Codex CLI is installed (before, the
  restart came first, and on a fresh hub every session failed to start with "the bans do not hold"). A failed install
  restarts nothing: the band says why and has "Retry". A hub on Codex that came up before Codex CLI was there finds it
  by itself as soon as it is installed: sessions start without a restart.
- A session's words sent straight to your own hub (`hub send liaison --to <hub> "…"`, no liaison in between) are back in
  its feed: the "Thread with hub X" block starts with them, and the other hub's answer joins it. Before, only the answers
  stayed in the thread. Words sent before the update stay missing from the feed. Needs `hub restart web`.
- `hub secret add --from-file <file>` now ends with a line about the file it read: it still holds the value in plain
  text, so delete it if it was made only to hand the value over, and keep it (knowing it will not change by itself when
  the secret is changed in the hub) if other programs read it, such as a service or docker compose. The core deletes
  nothing itself, and the line names the path only, never the value; a value from stdin gets no such line.
  — thanks: @GreatMedivack
- Clean: the icon picker no longer scrolls sideways and does not cut off its last column where the scrollbar is always
  drawn (Windows, Linux, a Mac set to "always show scrollbars"): the four columns share the width the bar leaves. And one
  failed request for the characters' pictures no longer switches them off until the page is reloaded: it is asked again
  after 2 and 6 seconds, and while they are missing, every 30 seconds; when one loads, the characters are back. Reload the
  page (⌘R / Ctrl+R). — thanks: @kalimullin
- A session whose turn was cut by a limit has "Resume" in its ⋯ menu: the core continues the cut turn now instead of at
  the planned time (the core's `POST /api/session/resume`, API 1.36); while the limit is still closed, the page says so
  and the session keeps waiting. Reload the page (⌘R / Ctrl+R). — thanks: @visualpharm
- Windows, the bus between hubs: the hub check ("keys or library not in order: TimeoutExpired ssh-keygen") and accepting
  an invite no longer hang 30 s and fail when a password manager's SSH agent (Bitwarden, 1Password) is running: the bus
  key has no passphrase, so `ssh-keygen` now signs without asking an SSH agent (on macOS and Linux too, where an agent
  that never answers held it the same way). Needs a hub restart. — thanks: @Eugenius-S
- The liaison no longer lights "new" for every letter it sends or passes on ("the words of X went to the hub Y", "passed
  to session X"): the session whose words went out, or which got the item, tells you itself. The liaison's result stays
  in its own feed and in the bus thread as before. It stays "new" when the turn did anything else: the core or the
  other hub refused something, a message is not sent or an item not delivered yet, the liaison asked you with a card,
  wrote to another hub in its own words for nobody, closed or declined an item, wrote down a rule, or answered in your
  chats with people; when the turn was on your own words, when it sent or passed nothing, and when that session is
  asleep. Needs `hub restart`. — thanks: @GreatMedivack
- The one-line install on macOS and Linux looks like an install now, not a pile of lines: five numbered steps (check the
  computer, download, unpack, set up, start) each with its time, the download as one live line with a bar, the
  megabytes and the speed, the hub's sign beside the words (the white dots of its orange tile glow in a wave while the work goes on, the
  sign is whole at the end and dimmed with a dark ray when a step fails), and a closing screen with the address of the
  page and the command. The colours are for a
  terminal that has them (24 bits, 256 or 16). With `NO_COLOR`, a `TERM` of `dumb`, a pipe or a log, a locale that is
  not UTF-8 or a window under 60 columns wide, and when the drawing does not work on a machine, the install says the plain
  lines it said before, the same way; `HUB_INSTALL_LOOK=plain` before the line asks for them too. Windows is as it was.
- Sessions with a queue (the hub, a mini-hub, a lead, a direction) no longer end every answer with a standing "Queue:"
  list copied from their earlier answers or kept by a summary after compaction, half of it long closed: each item is
  checked where it lives in that turn (the task, the session, the schedule, the MR), a closed one is dropped, and the
  end of an answer has only what changed and what waits for you. On three recorded turns, closed items named as open
  went from 7–9 of 9 to none, at the same cost; the rule of going through the queue stays as it was. Needs
  `hub restart`; sessions already running get the rule at their next start and by the text for sessions below.

### For sessions
- `hub tools grant <session> <tool> --for mN|qN` of a risky tool: the owner's message, or the question of the card they
  answered, must name the tool by its id ("the browser" does not). When the yes to a card should also connect a risky
  tool, name its id in the question (`browser:mac`): the answer is then the yes to it. Otherwise grant it without
  `--for` — the core asks the owner.
- On a hub on Codex the model of `hub session model` and of `--model` is `codex/<id>` (a bare id of the hub's Codex
  model list is accepted too); on a hub entirely on Codex Claude names do not apply.
- Work you hand off (a task, an investigation, a review someone waits for) goes to the hub's executors (`hub task
  run`); your own helper agents (Agent, Codex subagents) are for support work inside your turn — a search, reading, a
  parallel check — and you gather their findings in the same turn. A refused `hub task run` is fixed as the refusal
  says or reported, not moved to helpers.
- With a queue: do not end an answer with a standing "Queue:" list: not one copied from your earlier answers, not one a
  summary after compaction tells you to keep ("every reply ends with a Queue list" is no rule). Every item of your
  queue — from your earlier answer or a summary too — you check at its source in this turn (`hub task list`, `hub task
  show <dN>`, `hub show <session>`, `hub schedule list`, the MR, the cluster); closed — drop it; an item from an earlier
  answer or a summary is a reason to check, not a fact. The end of an answer has only what changed in this turn and, if
  something waits for the owner, "Waits for you: <what to decide>".

## 2026-11-18
- Mac, the open release: the hub runs through its own launcher, `Hub by Icons8.app` (Login Items show "Hub by Icons8"),
  built from the same launcher source, universal (Apple Silicon and Intel) and signed ad hoc, with no company
  certificate. An update does not swap the launcher of a hub that runs: to switch, run the install line again (a clone:
  `deploy/mac/install.sh`); it puts it in place of the earlier `Agents Hub.app`, and macOS asks once more for access
  to protected folders. Other builds keep `Agents Hub.app` as it was.
- Page, "Settings → Accounts": turning the account gateway off asks once more ("Turn it off? … untick it once more"), and
  that question no longer goes away with the next update of the page, so the second click really turns it off; the note
  after a click ("Turning on: the hub is checking claude") stays until the gateway's state changes. Reload the page
  (⌘R / Ctrl+R).
- A mini-hub's status while its task is with executors reads plainly: "waiting for executors · 2 working", "executor
  queued (1)" (nobody working yet), "executors: 2 working, 1 queued". Before: "waiting for executors · 0 + 1 queued".
  Page, `hub status` and the app show the same line; needs `hub restart web`.
- Page, the app's band: an update installed in the background that waits for your restart says "Update 1.9.6 is ready —
  restart the hub to switch to it" with a "Restart" button (the same restart as on the Updates screen). Before, the band
  said "Restarting for 1.9.6…" for hours, as if the restart were running. "Restarting for …" stays for a restart that was
  requested. Reload the page (⌘R / Ctrl+R).
- Page, "Skills and MCP": while the older history is still being read (the first minutes after an update) the last-call
  column shows a dash, not "Not since …" on every row; a repo skill says whose repo it is (`Repo maps`), not just
  "Repo"; skills that are not in the set now and were not called in the period are folded into one line; a skill named
  through a sibling working copy (`.claude/worktrees/<copy>:<skill>`) counts as the skill itself; the MCP status of a
  sleeping session says it is from the last run; the view waits ("Loading…") while the hub is still reading its journal
  instead of "Could not load". Reload the page (⌘R / Ctrl+R); the count of the working-copy names needs `hub restart web`.
- Page: "Now" holds only the external hubs that wrote within the last day (and the ones waiting for you): before, a browser
  with no mark of its own listed every hub that had written in two weeks. An older word stays in "All"; the dot of a row
  there follows the same rule. Reload the page (⌘R / Ctrl+R).
- Page, Clean on a phone: the Settings section bar no longer flips between the "☰" button and the full bar. Some loads
  left the bar 600 px wide on a 390 px screen, so the column scrolled sideways. Reload the page (⌘R / Ctrl+R).
- Page: "Now" lists an external hub again when it wrote something you have not seen — an answer of its sessions in a
  thread too, not only a word of its owner; the row has a dot until you open the hub. Since the new "Now" a hub whose
  agents answered did not come there at all. Needs `hub restart web`, then reload the page (⌘R / Ctrl+R).
- Plugins: when a catalog moves an entry to another repo (for example, to a copy the catalog keeps itself), "Update"
  updates the plugin you have in place, with its settings and its on/off switch. Before, it installed a second copy
  next to the first one, or refused with "another plugin with the same key".
- What "Sort into groups" got — "Task sent to the hub" or why it was not sent — shows as a line right under the group
  buttons for 10 seconds, on a phone and on a desktop. Before, it went to the banner: on a phone's session list the
  banner is hidden and the press seemed to do nothing, on a desktop it was far from the button. Reload the page (⌘R / Ctrl+R).
  — thanks: @visualpharm
- Page: the menu of a session's row in the list (right click, a long press on a phone) has the session's own actions
  too, as its header's "More": stop the turn, stop an executor, put to sleep, dismiss an agent, remove a resting one
  from the list, with the same confirmations. Before, it only moved the session to a group and changed its icon.
  — thanks: @visualpharm
- On a hub with Codex, "Settings → Codex" offers the critic right after the sign-in: "Critic: another model checks
  important decisions before they reach you" and "Enable" (the same switch as on the Modules card; once it is on, the row
  just says so). The critic on a Codex hub now asks a model your sign-in has: when your plan lacks the default critic
  model, it takes the model Codex marks as its default, and an effort that model knows — instead of a refusal on every
  call. A model named in `critic.model` stays as you wrote it. Reload the page (⌘R / Ctrl+R).
- Page: the Clean look and the chosen text size are on before the page is first drawn: the page no longer opens in the
  old layout for a moment and then jumps into Clean (on a desktop the whole sidebar and feed moved on every load and
  reload). Reload the page (⌘R / Ctrl+R). — thanks: @visualpharm
- The public build of the hub has no pool of Codex accounts: a hub on Codex there runs on its one sign-in (Settings →
  Codex, `hub codex login`), `hub codex` knows `status`, `login` and `logout`, and the page shows no "Add a Codex
  account". A hub that has the pool keeps it as it was. The README no longer promises a pool of Claude accounts or of
  Codex accounts, which the public build has neither of. `hub codex --help` shows its usage line instead of a catalog
  key. Nothing to do to update.
  — thanks: @visualpharm
- A hub installed from git has one update channel: `main` (nightly), everything new at once, with the same merge and
  tests in a separate copy as before. The `stable` channel (release tags with a 24-hour hold) is gone, and so is the
  channel row in Settings → Updates. A hub that was on `stable` moves to `main` by itself with this update; an
  `update.channel` left in hub.json is not read. Release tags stay as the marks of packaged releases; a hub from a
  package updates by its packages as before. The tag watcher of the hub that publishes the core is gone too (its
  messages about foreign release tags and its `hub doctor` row): nothing installs a tag by itself any more. Nothing to
  do to update.
- For builds of the core with their own code (Embed API 1.1): a Module API module put into
  `embed/<name>/modules/<module>/module.json` is a built-in module of the hub, like the core's own — it starts by its
  manifest (`"default_off": true` keeps it off until `modules.enabled` names it), with no `hub module add` after the
  install, and it stays found when the code moves to a new layer. A module named as one of the core's is refused with
  the reason in `hub modules` (the core's stays), and the build refuses such an embed too. A `modules.paths` entry that
  points at that folder (from an earlier `hub module add`) is now the same module, not a copy. Settings → Modules lists
  such modules with the third-party ones, with "Enable"/"Disable" (off is kept in `modules.off`); `hub module dev`
  works on them; their `catalog.py` adds tools to the catalog; the build refuses a manifest the hub would block.
  The page server needs a restart (`hub restart web`).
- Page: when you ask a session to find something out from another hub (in your words to it or in your task), its
  report of that hub's answer is in "Answered you", as an executor's answer to your task, and goes to Telegram if
  `telegram.page_results` is on. Before, it went to the folded "Sent on their own" and was gone from "Now" in a day.
  An answer to a request the session sent on its own, and news another hub sends unasked, stay where they were.
  Needs `hub restart`.
- Sessions open the fitting skill first: every session's instructions now end with a line to check the skills of its set
  before the first step and, if the task falls under one (data through an MCP server, a deploy, a manifest, the repo's work
  procedure), to open it with the Skill tool and work by it, not by an older recipe in its own file or memory. In the
  check, the fitting skill was opened in 8 of 9 runs instead of 2 of 9. Applies to sessions started or reloaded after the
  update (`hub session reload`). A Codex session gets the same line saying to read the skill's SKILL.md (Codex has no
  Skill tool).
- `hub session kit [<session>|--fleet] [--days 7|30|all] [--json]`: a session's skills and MCP as its engine loaded them
  against what was really opened — the data of the page's "Skills and MCP", without the page. For a session: per skill,
  how often it was opened (a Skill step, a `/command`, a read of its SKILL.md), in how many of the period's conversations,
  when last, and what its entry takes of the model's list; the skills never opened, with that weight; the MCP servers
  with their calls; the period's conversations and turns. `--fleet` is the same for the whole hub: in how many sessions
  each skill and server is loaded and how many opened it. Without a name — yourself. A session's set now says how many
  tokens its skill list takes of the model's context (Claude Code counts it; Codex does not — a dash), and the page's
  "Skills and MCP" shows that and a line "In the period: conversations N, turns M" under the set. The first call after
  the update reads the journal archives once (up to a minute; the page does it in the background). The page needs
  `hub restart web`; the tokens of a session's list appear once its process starts anew.
- Sessions: a step only you can do (a sign-in with a password that is not in Secrets, Touch ID, a code from your phone,
  a CAPTCHA) now comes as one card with the exact steps, not as a line at the end of a result ("sign in and write
  'done'"). For a password the first option is "I'll add it to Secrets" (Settings → Secrets; the session then carries on
  itself), the second is "I'll sign in myself". While the card waits, the session's results no longer repeat the step.
  A new session gets the rule at once; a running one — after its next restart.
- Page: "Now" keeps the external hubs by the same rules as sessions. A hub that answered you — your own words to it,
  or a request a session sent on your word — stays there until you open it, and its row says "answered you". A hub
  that wrote something on its own stays for a day; the one you just opened stays for 15 minutes, as before. Opening a
  hub counts on every device, as a session's "read" does: the phone does not bring back a hub you read on the computer.
  Before, every hub left "Now" a day after its last word, answers to you too. Needs `hub restart`, then reload the
  page (⌘R / Ctrl+R).
- When your task to a session breaks on an error and the session sends its report anyway — after the hub asks it
  "where is the result?" or the core tells it to continue — that report is in "Answered you" (and goes to Telegram if
  `telegram.page_results` is on), as the task's answer would have been. Within three hours of the break; a short "ok"
  or a line starting with "—" does not count. Before, it went to the folded "Sent on their own". Needs `hub restart`.
- Clean: the option "Waiting for you square, as in Classic" (Settings → Look, under Clean, off by default) puts a small
  square next to the search field with the number of sessions waiting for you; a click opens the first of them, as
  Classic's red button. It stays in view during a search and with the "Now" filter. Off, nothing changes. Page only:
  reload it (⌘R / Ctrl+R).

## 2026-11-17
- The hub's Telegram bot can work in a group of several people instead of your private chat: whoever is an admin of the
  group speaks for the owner (words, buttons, /stop, /away), the other members only ask — they get answers, but cannot
  press a card, give a permission or steer a session, and a session asks an admin with a form before acting on a
  member's words. Rights are checked with Telegram on every word and press. Topics of the group can be given to
  sessions: `/here <session>` in a topic sends its words there and brings that session's results and questions back;
  the rest goes to General and the hub. To turn it on: add the bot to the group as an admin with only "Delete messages"
  (not "Add new admins"), write `/start` in the group, run `hub-telegram pair <code>` in the hub's terminal — it shows
  the group and its admins and asks before it binds. `hub-telegram group off` brings the bot back to your private chat.
  In a group the bot does not answer private chats, and chats with people through Telegram are off. Without a group
  nothing changes. Needs a restart of the Telegram module after the update.
- "Now" in the session list is shorter and says what is in each row. A result that answers your words or your task is
  "Answered you" until you open the session, and its line is that answer even when the session sent something else
  later. A result a session sent on its own (another session's news, a schedule, a task handed in) is "Sent on their
  own": folded, with the count and "Mark all read" on its heading, and it leaves "Now" after a day (the dot in "All"
  stays). The watchdog's "reply?" is "Waiting for you", and its line is the action asked of you. Rows show two lines of
  text (the whole text is in the tooltip) and, right of the name, how long it waits or when the answer came. Linked
  hubs stand in "Now" only while one waits for you or has new messages; all of them are in "All". The turn in which an
  agent only records an executor's lessons no longer lights "new" or replaces the line ("MR opened" stays, not
  "recorded the lessons"). Clean keeps following its "Detailed session list" switch. For plugins and apps: a session row
  of the snapshot has `reply_head` and `reply_ts`, the start and time of the latest answer to the owner. Needs
  `hub restart`; reload the page (⌘R / Ctrl+R).
- No more "the liaison has not decided in 30 minutes" about a message from another hub that the liaison asked you
  about: while its question is open, the message waits for you, not for the liaison. Once you answer, the liaison has
  30 minutes to act on your answer before the notice.
- A session's message to your own hub goes out at once, without the liaison: `hub send liaison --to <hub>` to a hub
  whose pair code is checked, bound to a person of your contacts and with no liaison rule about it is sent by the
  core itself — the liaison used to spend a turn just to send the session's words as they were. Any other hub (a new
  person, one with a rule) still goes through the liaison.
- The liaison no longer carries a long conversation: after a turn past 60 thousand tokens the core gives it a new one
  (`/clear`), as you would. Its rules and profile come with every message anyway; its turns get cheaper. Not while
  its own question to you is open.
- A hub nobody sits at can be run from the hubs linked to it: Settings → External hubs → "Nobody sits at this hub's
  page — the linked hubs run it" (or `hub bus mode remote` in its terminal). Then the words of every linked hub with
  the pair code checked go straight to the hub session as its owner's task, without the liaison; a session's question
  by a form, which nobody would see, is refused and says whom to ask in the thread. These words do not count as the
  owner's for installing plugins, secrets or contacts — that stays with the hub's own terminal. Off by default.
- The agents' browser window is told from yours: a new browser profile the hub creates for a window (not a headless
  one) gets a green Chrome colour theme. One Chrome.app has one icon in the dock, and a visible agents' window was taken
  for your own. Only new profiles get the colour: an existing one, yours included, is not touched, and you can change
  the colour in Chrome ("Customize Chrome") — the hub leaves it.
- A signed-in site no longer drops you after the agents' browser restarts. A new browser profile (headless too) is
  marked by the hub as its own (a `hub-seeded` file in the profile folder), and the hub starts its Chrome with
  `--restore-last-session`: the tabs come back with their session cookies (the sign-ins without "remember me"), and the
  empty start tab is not added next to them. When the hub stops its Chrome (`hub restart browser`, an idle close, an update
  of Chrome) it asks Chrome to close itself first and signals it only if it does not leave: a cookie set a few seconds
  before a signal was lost. Existing profiles are not changed, yours included: for one of them turn on "Continue where you
  left off" in Chrome's settings ("On startup"). After a crash Chrome restores no tabs, the cookies stay.
- Sessions can put a secret into Settings → Secrets themselves: `hub secret add <name> --from-file <file> --key KEY`
  (a .env line or a JSON field; a file of one line as it is; `-` for stdin) — the value goes from the file into the
  vault unseen, the command prints only the name and the key. A new secret takes its sites and "into commands" from the
  command; an existing one only a new value — its sites and switch stay yours. The secret's card says which session
  added or changed it and from which file, and "Change" lists its last three values with "Return". Removing stays yours
  alone. Every addition is a line in the session's feed. Needs `hub restart`.
- A theme's settings are next to the theme: in Settings → Look they stand right under the row of the chosen theme, and
  with another theme chosen they are not shown at all. The section "Clean theme" at the bottom of "Look" is gone: Clean's
  ticks (detailed list, avatars, sorting, counts, usage bars) are under the Clean row, with your choices as they were.
  A plugin's theme can bring its own settings the same way (Plugin API 1.37, `hub.addOptions({ themes })`).
- A key at the start of a session's turn no longer goes into the journal: known key forms (tokens, a password in an
  address) are masked there as they already were in tools' input and output. The case: a background `hub run` ended,
  and Claude Code's notice about it quoted the command with a token in it. The command and the environment of a `hub run`
  are no longer kept in its folder after it starts (the folder stays for days). What earlier versions wrote stays where
  it is: if you think a key got into the journal, run `hub journal scrub` in a terminal (it counts first; `--apply`
  cleans) — thanks: @GreatMedivack
- The browser's "translate this page" no longer rewrites the row under the input field: it turned model names and
  effort levels into other words (a model name into a common noun, an effort level into a word of its own) and a chip's word into another. The row, its
  chips and the model and effort menus are marked as not to be translated, and so are the model and effort lists in the
  settings (executors); the rest of the page is as before.
- `hub direction <name> doc update … --content ""` no longer wipes a direction's document: an empty or blank `--content`
  (a script that fills it failed and left an empty substitution) is refused with the reason, and nothing is written;
  `doc create` refuses the same. Without `--content` both work as before.
- An archive made by the journal's rotation while the page is already running is now parsed in the background at once.
  Before, only the page's start and scrolling the feed up started the parse, so for hours `hub show --grep` skipped the
  new archive as "not parsed yet" until someone scrolled — thanks: @GreatMedivack
- "Settings → Accounts": the account cards no longer cut their text at the edge. In two columns (and at a larger text step)
  the limit's "resets …" went under the card's edge and the tab got a sideways scroll; now a narrow card puts the reset
  under the bar, and a card with room keeps it beside the percent. The email is shown whole next to the name (the badge
  "busy until …" or "Switch" drops under it instead of squeezing it to "me@…"), and "Refresh" drops under the
  "measured …" line instead of wrapping it into three lines.
- In the Clean look, the "Now" button of a message waiting in the queue has an arrow-into-a-frame icon (the message goes
  into the running turn). Before, it was a curved U-turn arrow that read as "replay".
- Tests: on macOS `tests.test_secrets` no longer goes red — the Secrets tab tests reached the machine's Keychain through
  `security` under the tests' HOME (no login Keychain there: the add hung until the request timed out), and the
  two-hubs test gave both hubs the main hub's Keychain account. The tests now keep a Keychain of their own, and the
  second hub is an instance with a name. No restart needed — thanks: @visualpharm
- Telegram: an answer from Telegram that is cut off (or a proxy's reply that is not HTTP at all) no longer breaks the
  bot's cycle. Such a failure is now a network one, like a dropped connection: the message or file is kept and sent
  again, and a failed attachment download is reported in the chat instead of stopping the cycle. Needs
  `hub restart telegram`. A file from Telegram that arrives cut short (the connection breaks before the promised
  length) is no longer taken for the whole file: it counts as a failed download and is reported in the chat.
- A thumbnail (`/api/file?path=…&w=<px>`) of a picture with an alpha channel — a window screenshot with its shadow, a logo
  on a clear background — is a png and keeps its transparency; it was a jpeg with the clear area painted white, a white
  box on a dark page. Pictures without one are still jpegs; the widths on offer are the same. The answer says
  `X-Hub-Thumb`: the copy's width, or `0` when the original came (a client need not read the `ETag` for that; Plugin
  API 1.37). No restart needed beyond the update's own. — thanks: @visualpharm
- The bus relay (`relay/server.py`) can be open to everyone: `server.py open` prints one line any hub registers by
  (a year by default, `--days N`; asked again — the same line), `server.py open --off` closes it, and the hubs that came
  by it stay. `server.py bootstrap` asked again before the first hub came prints the same line instead of refusing.
  The relay carries more hubs: a letter wakes only its receiver's long poll (every poll used to look into the database
  on each letter), and with the cryptography library installed (the image has it) the relay signs and checks in its
  own process — the same signatures as ssh-keygen writes, no two runs of it a request. New settings:
  `HUB_RELAY_POLLS` (long polls at once, 64) and `HUB_RELAY_JOINS_DAY` (joins by members' links a day, 50; 0 — no
  cap). Hubs need nothing: the protocol is the same; a relay you run yourself takes the new code with its next image.
- A hub installed by the public line links with other hubs at once: "Link with a hub" (or `hub bus link`) on a hub
  that is on no relay yet takes the public relay its install names — the open build's pages carry its line
  (`v1/relay.json`) — so nothing has to be pasted first. The page and the terminal name that relay before the press;
  your own relay instead: paste its line into "Accept an invitation" (or `hub bus join -`) before the first link.
  Hubs of the team build and hubs from git are as before. For the release: `tools/package/pages.py` needs
  `--relay <file with the line>` (or `--relay none`).
- Your own bus relay in one command: `curl -fsSL <the release's pages>/relay.sh | sh -s relay.example.com` on a Linux
  server with Docker (or `sh relay/install.sh <name>` from a copy of the repository) starts the relay of that release
  with HTTPS in front of it (Caddy, Let's Encrypt) and prints the line of your first hub — paste it into "Accept an
  invitation" before that hub links with anyone. Run again, it keeps the project and repeats the line until the first
  hub came. See relay/README.md, "Your own relay".
- Sessions no longer wait on an undatable event: a prod or irreversible step that hangs on one (a customer's payment,
  someone else's answer) goes to you as a card at once, and an answer awaited for over ~12 h gets one reminder.
- Taking something off no longer needs a second card: when you tell a session "remove the plugin", it passes this to the
  hub, and the hub's `hub marketplace uninstall|remove`, `hub module remove` and `hub market remove` take your message to
  that session as your word. Installing, granting and launching still need a word to the hub itself or an answer to a card.
- A bus relay open to everyone has limits that keep it up for everyone: joins by the open line — 500 a day for the
  relay and 5 a day from one address (`HUB_RELAY_OPEN_DAY`, `HUB_RELAY_OPEN_ADDR_DAY`; behind a proxy it needs
  `HUB_RELAY_TRUST_PROXY=1`); a hub's signed requests — 300 at once, 120 a minute (`HUB_RELAY_HUB_BURST`,
  `HUB_RELAY_HUB_MINUTE`; a hub's normal pace and a backlog after a cut stay far below); one sender's introductions —
  10 waiting for a hub and 100 hubs a day; the database — a ceiling of 8 GiB in use (`HUB_RELAY_DB_MAX_MB`): over it new
  letters, hubs and invitations wait (`full`), reading goes on. Invitations used over 30 days ago are dropped. The image
  runs as user 10001 by number, and its health check fails when the cryptography library does not load (`serve` says
  so; `HUB_RELAY_SIGNER=ssh-keygen` chooses ssh-keygen). SIGTERM stops the relay at once, also as a container's
  PID 1 (a pod restarted in seconds, not after its grace period), and `HUB_RELAY_PORT=tcp://…` that Kubernetes sets
  for a Service named hub-relay no longer stops it from starting. Hubs need nothing: a join refused by a limit is said
  in words (try later), a letter refused for space is handed again by the hub itself. A relay you run yourself takes
  it with its next image.
- Clean: "Narrow feed" in "Settings → Look" (under the Clean row, off by default) keeps the feed and the input box in a
  760 px column in the middle of the window, as in chats; off, they fill the window up to 1104 px as before.
- Clean: with the session list hidden, the list button in the corner stays on the title row whatever stands above the
  header — plugin icons in the top strip no longer put the button over them.
- A session that waits to start takes the account pool as it is when it starts, not as it was when the start was decided.
  Waiting for the sign-in ("Claude Code is not signed in — sessions spawn once it is"), for the previous process, for
  modules, the login module, the gateway or a folder move, it used to keep the pool of that moment: an account added
  meanwhile (`hub accounts add`, a switch) was not used until a hub restart, and the start asked Claude Code about a
  sign-in it had already been given. Now every pass takes the pool's account anew and the session goes at once, as one
  that was there from the start; an account removed meanwhile is not used. A hub that waited for `claude auth login` was
  not affected and works as before. Needs `hub restart`.
- `hub bus relay join -` (and `--file <path>`) reads the invitation from stdin like `hub bus join -`, so it stays out
  of the shell history and `ps`. A refused invitation now says why: a line that is not an invitation asks to paste the
  whole `hub-relay-invite-1:…`; only an invitation the relay really marked as used still warns that it may have been
  intercepted.
- Plugin icons in the header of the session list (next to the search field) take at most two fifths of the row and wrap
  into lines: six icons no longer squeeze the search field to a few pixels, in Clean and in Classic alike. One to three
  icons look as before.
- With the VPN off a turn no longer takes the pool for used up. Claude Code's "Failed to authenticate. API Error: 403
  Request not allowed" (the API refusing a network it does not allow) used to count as a refused sign-in: the account
  was marked busy, a fresh token was taken for nothing, and the owner heard "none free" with a reset time while the
  limit was almost empty. Now such a turn is a network cut: the account stays as it is, the session continues once the
  API answers, and the status line says "no access to the Claude API from this network (looks like a VPN), the account
  is not at fault". Other 403s, 401, a revoked token and a suspension are refusals as before. The "none free" notice
  says what holds the account: a refused sign-in gives the time it is checked again, a token command that gives
  nothing says there is no connection to the token issuer (a command that does not answer in 60 s says so by its
  wait), a real limit stays the nearest reset. Such a session waits for the VPN: the core asks the API with a request it
  refuses from a network it does not allow (no key, no model called), continues the turn only once the answer is
  something else, and spends none of its repeats while it waits. Needs `hub restart` — thanks: @GreatMedivack
- Clean on a phone: the gray tab over the message field shows the session's folder name on the right, as the
  computer shows its path; a long name ends with "…" and leaves the conversation size whole. A tab with nothing to
  show (no folder, no conversation size yet) is not drawn: it was an empty gray strip. — thanks: @visualpharm
- `hub-telegram pair` and `hub-telegram token` (they decide whom the bot obeys) refuse to run from a hub session or a
  plugin or module process and say it is the owner's command for their own terminal; before, a session with the code
  in hand could bind anyone as the owner. In the owner's terminal they work as before.
- README names the hub's author at the top and ends with a "Contributors" section; CONTRIBUTORS.md lists everyone
  whose pull requests, bug reports and ideas went into the hub, most first. Both are built at every release from the
  thanks in this file (`tools/contributors.py`). The release commit credits as co-authors those thanked since the
  previous one (the first commit of a new repository: everyone), which is how GitHub counts them among the
  repository's contributors. Nothing to do.
- The feed folds less: "Show all" is only under a message taller than two thirds of the feed (it used to cut any message
  of another session past 280 characters to three lines), and a message once shown whole stays whole when new ones come
  or the window gets smaller. An answered reply no longer turns gray and folds when your next message goes, the
  "Reactions" module on or not; "Fold the answered" in Settings → Look brings that back. In "Brief" every reply of a
  turn stays in view, each under its steps: a reply you started reading is not hidden when the session adds "Saved to
  memory" after it. Under a folded message "Show all" and "Show less" in Clean read as a button, the same as "More" in
  the header (a chevron, a light fill under the mouse), with a finger-sized touch on a phone (it was a small gray
  "more"). — thanks: @visualpharm
- Web, message field: a long paste (over 20,000 characters or 400 lines — a log, an article) is attached as a text file
  with its start in a small tile, the field stays free; a short paste stays text. The field no longer repeats the
  session's name — it is in the header. Replying to a quote puts it into the field as a card: up to two lines of the
  quote as plain words (no markdown marks, as in the feed), instead of "Cancel". The quote and the attachments are one
  strip above the text, as in the apps. Their ✕ is a visible disc in the corner (22 px on a phone, 20 with a mouse),
  with a 44 px tap area on a phone and 28 with a mouse; the paperclip stays above those areas. — thanks: @visualpharm
- In the feed, a question from a member of the hub's Telegram group is signed with their name and stands on the left
  like any other person's words, in a colour of its own. Before, it looked like your own bubble "You · Telegram", with
  a reaction on it. An admin's words are still yours. A page next to an older core shows the same row under "Group
  member". Needs `hub restart`.
- Clean catches up with the design review of its plugin version (seventeen changes, Clean only; Classic and the other
  themes look the same). The look: soft plates by alpha, one button size (24), Danger as a soft red, a darker red
  (4.5:1 in both variants), underlined links, focus rings 2 px off the edge, disabled in grey, the account icon from the
  Icons8 Forma pack. The session list is quieter: no executor count unless "Counts in the list" is on, the "reply?" tag
  no longer covers the time, the "External hubs" section and its tab. In the feed: Reply and Reaction stand at the
  message and stay off the text on a phone, service rows, a turn error, the lightbox and step names, a question card
  whose answer field is one plate, a board with fact plates, card picking, loading and a load error. The input field:
  its message (a send error, a wait for an upload, away strips, a voice recording) is a plate inside the field. The
  setup and MCP sign-in bands are one quiet row, "No connection" has Retry. Settings: the tabs are cards and on/off
  settings are toggles. Menus: ☰, the header menus, the "/" commands and the schedule chip. On a phone: a one-row header,
  "More" and the menus are sheets from the bottom, feed search is in the header. Typing in the field no longer
  restyles and re-lays out the whole page. The copy button of a code block carries `data-state` (copy, ok, fail) for the
  theme — thanks: @kalimullin

- A browser that starts on demand no longer opens a second Chrome (and an extra empty window) when the process list is
  slow or missing (Windows, a loaded Mac): the launcher also asks the profile itself, whether its lock is held by a live
  Chrome. A lock left by a dead Chrome does not hold anything. Needs `hub restart`. — thanks: @Eugenius-S
- Clean: the open list of the Settings sections (☰ in a narrow column) no longer twitches and fades in again while you
  move the mouse over it: the page redraws Settings on every snapshot, and the list used to be built anew each time.
  Now it stays the same list and fades in only when it opens.
- The session icon picker has a search: type a word in the field above the grid (a name or a keyword, in the language
  of the hub or in English: "rocket", "coffee", and the same words in your language) and every emoji that fits is
  shown, not only the 32 of the grid. A click sets it as the icon; Enter takes the first one found; Esc clears the field
  (the next Esc closes the picker). On a computer the field has the focus as soon as the picker opens, on a phone it
  does not, so the keyboard stays closed until you tap it. The names are Unicode CLDR annotations in all 13 languages
  of the hub; they are loaded from the hub at the first letter typed (about 100–250 KB per language, English too), not
  with the page. No restart needed, reload the page.
- A hub nobody sits at but with a Telegram group bound: a session's question by a form goes to the group as a usual
  card, and its admins answer it, instead of being refused. A question on a task from a linked hub still goes to that
  hub in its thread; with no group every form is refused, as before. Needs `hub restart`.
- A session that Opus's safety filter moves to Opus 4.8 now comes back to Opus 5.5 by itself. When the filter stops an
  answer twice, Claude Code finishes the turn on Opus 4.8 and stays there, across restarts too. Now the hub waits for
  the turn to end (its background work and an open question hold it), cuts that turn out of the conversation, so the
  filter does not see it again, and raises the session on the model it had; what the turn changed on disk stays. The
  session's header says what happened ("conversation cleaned, Opus 5.5 again") and how many times the filter moved
  sessions of the same account in a day. If it trips again within a day, the hub does not bring it back by itself: the
  session stays on Opus 4.8, its list line says it needs your hand, and the header's chip is the button that brings it
  back (the same as `hub session reload`). Needs `hub restart`.
- Accounts, a token from a command: a fresh token the service hands out already spent (refused from its first request)
  is replaced after a minute, then two, four, eight minutes and so on, up to the earlier interval (a quarter to half an
  hour), not after that whole interval at once; before, the fleet could stand for minutes after the service's other
  accounts had reset. `hub accounts use <account> --force` on the account that is already active takes a fresh token
  from its command (it used to keep the same spent one). A token that had reserve at its first reading is handled as
  before. Nothing to do; needs `hub restart`.
- Clean theme: a search in the session list with nothing found is now one line under the field, "Nothing found", as in
  the phone and Mac apps. The block with an icon, "No sessions with … in the name", "Check the spelling." and a "Clear
  search" button is gone: the cross in the field and Esc clear the search. The line has no full stop any more, in every
  language, and so in the other themes too. Static files only: reload the page, no `hub restart` — thanks: @visualpharm
- Account gateway: when the hub's own copy of Claude Code arrives after the gateway has started (the update downloads
  it a minute after the hub starts), the gateway checks that copy within seconds. Before, it kept checking the claude it
  found at its start, while sessions already ran the copy: every session start waited 90 seconds and then went past the
  gateway, until `hub restart gateway`. Needs `hub restart`.
- Account gateway: a `hub run` and a schedule job's command or check on a pool account get the same stand-in sign-in as
  sessions, and the gateway puts the account's token in. Before, their `claude -p` started with this computer's own
  sign-in: when that one had expired, it stopped with "OAuth session expired and could not be refreshed" before reaching
  the gateway. A job with its own API key, or with this computer's sign-in as the active account, runs as before. Needs
  `hub restart`.
- Hub bus: a session's words to another hub now continue the thread that hub's words came to it in — the one its turn
  is on, else the latest — instead of opening a new thread each round. Before, an answer that came straight to the
  session (in the thread of its own request) was answered in a new thread, so one topic spread over many threads and
  the other hub's liaison had to guess the session again. The core keeps a thread only while the answer in it would
  come back to the same session (alive, under 50 messages, opened by it or by nobody); the liaison's own `--thread`
  still wins. External hubs shows fewer threads per contact. Nothing to do; needs `hub restart`. A hub on the other
  end continues our threads the same way once it updates.
- The one-line install on Linux no longer stops with "install git": a machine without git (a fresh Debian, Ubuntu,
  Fedora ...) gets it from the line itself, through `sudo` and the system's package manager (apt, dnf, yum, zypper,
  pacman). When the line is piped, sudo asks for the password on the terminal; with no terminal, or no right to sudo,
  the old message with the command to run by hand stays. The same for `curl`, `tar`, `gzip` and the checksum tool.
  macOS and Windows need no git. Nothing to do for a hub already installed.
- One-line install, the line run again on Linux ARM, Windows and a Mac: the environment in place is taken as it is. It
  used to be called broken ("not as unpacked: …\__pycache__\….pyc": the environment's Python compiles its own library
  at the first install, and the check counted that as damage), so every run again downloaded 190–270 MB once more,
  moved the hub onto the new copy and restarted it (on Windows about six minutes). The check of the rest of the
  environment's files is as strict as before. Nothing to do: the next run of the line takes the new script itself.
- One-line install on Windows under a user whose name has letters outside the system's code page (a Cyrillic name on
  an English Windows, for example) and a space: the install no longer stops after the download with "the environment
  archive has no top folder (tar.exe -t)", nor a minute later at the step "service" (UnicodeEncodeError: the `hub`
  command and the hub's launcher now name such a folder through `%LOCALAPPDATA%`). Run the line again.
- The install log on Windows (`%LOCALAPPDATA%\Programs\agents-hub\install.log`) keeps dashes and non-Latin letters:
  what the hub's Windows installer said used to land there as "�" and "????". Nothing to do.
- The Start menu shortcut on Windows is called "Hub by Icons8", as on Linux and in the apps (it was "Agents Hub"): the
  next run of the line puts it in place of the old one, `hub uninstall` removes both.
- Hooks for themes: places a theme could not reach from CSS now carry a class or a data attribute — the "ask" and
  "terminal" rows of the feed, the sign in the "Another hub" chip, the failed-board line, the catalog load error in
  Plugins, the failed-removal row of the queue strip, the account gateway's note. Two things change to the eye: the
  error banner of "Files" is the error colour on the same plate (no orange stripe on its side), and the "Restart"
  button of the restart confirmation is a regular button, not a red one: a restart destroys nothing. Static files
  only: reload the page, no `hub restart`.
- The input field and the page got a few small buttons, in every theme (asked for by the designer of the Clean theme).
  A voice recording has "Done" next to the cross: it stops, and the recognized words go into the field to read and
  correct, nothing is sent (the red button still sends); while the speech is being recognized the recording's length and
  the cross stay on screen, and the cross lets the answer go. The "To" line has a cross while the message is addressed
  away from the session (a reply, a card): one press takes that off, as "Cancel" of the reply bar does. A board that
  did not load says what to do and has "Retry". On a phone the search over the feed is closed with the word "Cancel",
  the list says "No group actions while searching" in place of the group buttons while you search, and "Brief |
  Detailed" and "Feed | Board" are items of the "More" menu instead of buttons in the header (on a wide screen they stay
  in the header; the menu has them hidden, for a theme with a narrow header: Clean now uses these items). Static files
  only: reload the page, no `hub restart`.
- Short page labels and messages no longer end with a full stop, in every language.
- The hints of the view settings ("Brief" feed and the Clean list) no longer say "unchecked": a theme can draw the
  same setting as a switch, so they read "if off …" in every language. Texts only: reload the page, no `hub restart`.
- "Waits for you": a result that ends with "the release is on your yes; session X brings the card" no longer gets the
  "answer?" mark — the card itself comes to you, and before, the same ending got the mark or not depending on its
  wording. A result that asks for your yes right there still gets it. Takes effect after `hub restart`.
- Fewer "answer?" marks next to results. While a session has an open question card, its results get no mark: its row
  already waits for you with the card's question, and the mark mostly repeated the card. If it asks for something else
  too, that is weighed again in its result after your answer to the card. A mark you dismissed no longer comes back with
  the next result of the same session for an hour, unless you write to it or answer its card — sessions woken by other
  sessions end with the same list every few minutes. Takes effect after `hub restart`.
- A plugin's MCP server that refers to environment variables now comes up in a session: `${VAR}` and `${VAR:-default}`
  in its address, command, arguments, headers and env are filled in from the session's environment, as Claude Code does
  (before, the address went through as written and the server failed with "'url' is not a valid URL"). A server whose
  address, command or header refers to a variable that is not set (or empty) and has no default is not started — with
  one line in the hub log naming the variable (never its value) and no retry until the next launch — instead of
  connecting with an empty token and being refused, which also switched its sign-in off; the plugin's other servers
  are not affected. The same goes for a plugin's own settings (`${user_config.*}`): one that was never entered and has
  no `default` in the plugin's manifest keeps the server from starting (a plugin's default now applies) instead of the
  server exiting at once with "Connection closed". An env key is the one exception: if a variable of the same name is
  already in the session environment, the key is left out and the server reads it itself. Takes effect after
  `hub restart`.
- Skills out of the box: every hub connects the plugin catalogs of Claude Code (`claude-plugins-official`) and Codex
  (`openai-curated`) next to its own, and `hub marketplace add` takes any Claude Code or Codex marketplace, a repo that
  is one plugin, or a repo of `skills/<name>/SKILL.md` folders. Agent Developer finds a skill for an agent's task,
  installs it and gives it to the agent itself (`hub tools grant <agent> <plugin>@<catalog>` installs it on the way);
  so does the hub, without asking you. A plugin of text only is given at once. A plugin that runs code — a hook, an MCP
  or LSP server, a program in `bin/` or in a skill, a ChatGPT app — waits for your word: your message that names it, or
  the card, which now lists what will run. This is read from the plugin's files, not its author's words: plugins of
  your catalog with an MCP server or `bin/` are now given to new sessions only through you; what sessions already have
  stays. Every grant shows in the session's feed; take one back with `hub tools revoke <session> <id>`. To keep a
  catalog out: `hub marketplace remove <name>` — it does not come back. Downloads two catalogs in the background after
  the update (Codex's is about 100 MB).
- A session's "More" menu has "Skills and MCP": which skills and MCP servers its process really loaded and which it
  used, for 7 days, 30 days or all the time. A skill shows where it comes from (a plugin, the repo, Claude Code), when
  it was last called and whether it was called in the current conversation; "Not in the model's list" marks a skill
  only a /command can call. An agent sees its executors' calls next to its own, and an executor sees the skills its
  lead has and it does not (a working copy of a repo without the lead's plugins). An MCP tool's calls are split by
  what they opened (`name=…`); what a server holds and nobody called is not seen. A plugin that did not load is shown
  with Claude Code's reason. The first open reads the journal's archives once in the background (seconds to a minute).
  It replaces the "Tool: …" lines of the menu. Needs `hub restart`.
- Tests on Windows: the repo-rules shell test (`tests/test_codex_repo_rules.py`) is no longer red when the temp folder
  is on another disk than the folder the tests run from. Only the test's fictional home folder lacked a disk; a real
  home always has one, so the rules themselves behave the same there.
- A plugin that changes the page's look (a token such as `--column`, the width of the feed) no longer makes the page
  jump: its stylesheet used to arrive after the page had loaded the plugin's code, so the first frame had the standard
  width and the next one the plugin's. For plugin authors: list the files in the plugin's `hub-plugin.json`, `"css":
  ["web/look.css"]` (up to 8 `.css` files inside the plugin folder; the page puts them into its `<head>` after the hub's
  own, from the first byte), and `hub.addStyle` is no longer needed for them (a call for a listed file does nothing; a
  plugin installed while the page is open gets them too). API 1.37; `hub.addStyle` works as before. Needs `hub restart`.
- "I'm away" now means the hub decides for you: a question a session leaves for you while you are away goes to the
  hub, and the hub answers it in your place unless it is your own decision — money, prod and publishing, people,
  anything irreversible, a repo's gate, access and secrets wait for you as before. So do the questions that were open
  when you left. Back, the summary starts with "Decided for you by the hub" — each answer with its why and how to roll it
  back; to undo one, tell the hub ("undo q305, make it …"). A question marked 🚨 now reaches you at once even while you
  are away. The hub's answer is not your word: no gate opens with it. Needs `hub restart`.
- In Clean, the "New · N" line in the feed is back to the plate it had before: a solid "New · N" pill, a line and
  "since 14:20" next to it, in the theme colors, readable in light and dark. Reload the page (⌘R / Ctrl+R).
### For sessions
- Agent Developer: skills for an agent are yours — `hub marketplace search <words>` (Claude Code and Codex catalogs
  come out of the box), `hub marketplace add <repo>` for another catalog, `hub tools grant <agent> <plugin>@<catalog>`
  (installs it on the way); one that runs code goes to the owner as a card, or `--for mN` with his words that name it.
  Section 3a of the agent-developer skill. Another session asks Agent Developer or the hub; `hub tools request` as before.
- While the owner is away, what you leave for them in a card the hub may answer in their place; the answer comes as a
  message on that card. It is the hub's word, not the owner's — keep the step reversible.
- `hub send liaison --to <hub> --thread T "…"`: name the thread your words answer in when you have several
  matters with that hub (the thread id is in the frame of its message); without it the core picks the latest.
- A secret the owner points you to in a file (.env, an export) or that a command gives you goes into the hub's vault
  without opening the file: `hub secret add <name> --from-file <file> --key <KEY>` (`-` — stdin); without `--key` a
  file of several keys answers with their names only. See `hub secret --help`.
- `hub marketplace uninstall|remove`, `hub module remove`, `hub market remove` (the hub only): `--for mN` may be the
  owner's message to any session, not older than a day — pass it on from the session he wrote to; install, grant and run
  take only his message to you or `--for qN`.

## 2026-11-16
- The core's open edition starts with this release (LICENSE, NOTICE); it is not public yet.
- A hub's page can have a password: Settings → General → Hubs on this computer, "Password" in the hub's row (each hub
  of the computer its own), or `hub password set` in a terminal. With it, the page and "Files" of that hub open only
  after the password, from this computer, from the phone through the tailnet and through Cloudflare Access alike; a
  browser stays signed in while it opens the page at least once in 30 days, "Sign out" in the hub's row signs this
  browser out, a new password signs every device out. Five wrong tries lock the form for a minute, then longer. Off by
  default: without a password nothing changes. If one hub of the computer has a password and another has none, the
  list says so — a hub without one opens the others (its "Files" read the whole disk). Forgot it: `hub password off`
  in a terminal of that computer. The iOS and Android apps do not sign in yet: on a hub with a password they stop
  working until they learn it. The setting "Open the page only by a link with a key" and the hint about other user
  accounts of the computer are gone; a hub that had the key on keeps it, with its link and `hub ui`, until you set a
  password (a note in the feed says so), and `hub page-key` is replaced by `hub password`. Needs `hub restart`.
- Secrets: the hub keeps passwords and API keys itself, encrypted. Settings → Secrets: add one (a name, the value, an
  optional login, the sites it may go to, a note for agents), change it, remove it, "Copy" it to your clipboard — the
  value is never shown on the page. Agents use it without seeing it: `hub secret fill <name>` puts it into the password
  field where the cursor is, only on the secret's sites; `hub secret exec --env VAR=<name> -- <command>` gives it to a
  command in its environment and masks it in the output — only for secrets you allowed into commands (off by default).
  Every use is a line in the session's feed and "last used" on the card; no confirmation is asked. The values are
  sealed with AES-256-GCM in `secrets/vault.json` of the hub's folder; the key is outside it (macOS Keychain, DPAPI on
  Windows, a file in the settings folder on Linux), so a copy of the hub's folder does not open them. Through
  Cloudflare Access the page neither sends nor shows values. A hub from git without the `cryptography` library gets an
  "Install" button on the tab. The old allow list `secrets.items` in hub.json (references to outside vaults, which never
  worked) is no longer read; `secrets.enabled: false` turns the feature off. The vault guards against values leaking
  by accident — into agents' conversations, the journal, screenshots, copies of folders — not against a session set on
  getting one: sessions run as your user. Needs `hub restart`.
- A password or a key labelled in words of a language other than English now stays out of the journal and the page
  too: "Passwort: …", "contraseña - …", "mot de passe : …" and the same words in every language of the interface (they
  are language data, `i18n/data/<lang>.json`), in any case, with "=" or a dash, or with a few words or a bracket before
  the separator ("Password (admin page): …"). Before, only English names were recognised (`password=`, `token:`): such a
  value in a message a session sent through a chat MCP tool, or in the preview of the last message in the tool's list of
  chats, went into the journal as is. JSON output with escaped letters is masked too. A message to another hub or a
  chat reply holding such a value is now refused as key-like, as one with an English label was. Needs `hub restart`.
  — thanks: @Enwimo
- The "Modules" tab no longer says the hub works without all of its modules: the hub turns some of them on itself and
  they cannot be switched off (files, the schedule, directions, contacts, agents, the coach), and the tab now says so.
  Those cards carry a "needed by the hub" mark; the rest stay optional, with a switch or a setting. Needs `hub restart web`.
- Settings has no "Setup" tab any more, on any hub. What holds a hub (Claude Code, the sign-in to Claude, or to Codex
  on a hub on Codex) is the band at the top of the page, which now has the buttons itself on a hub from git too
  (install, sign in, the code field) instead of leading to a tab. What is not done is a card at the top of "About the
  hub": the agents' browser that is not there or did not download, the team's modules that are not connected, a hub
  check with warnings (with "Check again" and "A file for support"). When all is done there is nothing extra. The texts
  that sent to "Settings → Setup" (`hub doctor`, `hub setup`, the notice that Claude Code is missing) name the band.
  The hub check no longer warns "no sign-in of its own" on a hub whose own accounts pool has a usable account: that is
  its normal state ("sessions sign in through the hub's accounts"); with no account in the pool it is still a failure.
- "Usage" moved out of "Settings": it is now a page of the ☰ menu right after "Flow" (address `#/x/usage`), with the same
  content, as wide as the page, not a 880 px column. In the Clean theme the item has a Forma bar-chart icon. The old
  address of the tab (`hub.openSettings("x:usage")`) opens the new page. Needs `hub restart web`.
- Settings → Look → "Text size" now changes the font across the whole hub. The default look (Clean) drew all its text in
  fixed pixels, so "Large" and "Extra large" changed almost nothing there (only the earlier look, Classic, and the themes
  over it followed the setting): now the lists, the feed, cards, menus, Settings, Schedule, Updates and the rest grow
  by the same step, and the lines and boxes that hold the text grow with it. "Files" is another page on its own port
  and never saw the choice: the hub page now leaves it in a cookie of the host (none at the default step) and "Files"
  takes it before the first paint, in every look. At "Normal" nothing changes by a pixel. Reload the page (⌘R / Ctrl+R)
  after the update; no restart needed.
- A long session name in the Clean list is now cut with an ellipsis before whatever stands right of it (the red "waits for
  you" tag, the "reply?" tag, the failed cross, the unread dot, the working dots, the sleep icon, the counter of the
  executors) instead of running under it, also on the rows of executors under an agent; the full name is in the row tooltip.
- `hub show --grep` now finds what a session wrote that has already left its live feed but is not in an archive yet.
  The feed keeps the last 400 rows of a session and the terminal started from the last snapshot, so a result a day old
  could be in the journal file and still answer "nobody wrote about …". The search now reads every row of the journal
  file (the same fold as the page's, kept next to it like the one `hub show` has) and then the parsed archives, a row
  that lies in both counted once. When some archives are not parsed yet, or the time limit stopped the search, the answer
  says "nothing found in the part of the journal that was searched" next to the line that names them, not "nobody wrote".
  The first search after an update or a journal rotation takes about as long as the first `hub show` does (measured
  on a 50 MB journal: no longer). Needs no restart.
- A Codex sign-in whose token is dead no longer traps the hub. After a revoked refresh token ("log out and sign in
  again", "unauthorized (401)") "Sign out" in Settings → Codex and `hub codex logout` failed with the same error and
  `hub codex login` stopped at reading the old account, so the only way out was to delete the folder's `auth.json` by
  hand. Now a sign-out with a dead token deletes that file itself (only it: the folder's settings and threads stay) and
  ends at "not signed in", and a sign-in whose old account cannot be read starts from a clean folder and shows the
  code. A network or server error is not taken for a dead token: the file stays and the command says what failed.
  Needs a `hub restart` for the page.
- A module or a plugin can no longer insert a message into a busy session through `message.inject` without the right:
  before, it could send an ordinary message and then push it into the middle of the session's turn, past the queue and
  the rule that only the hub or the owner sends urgently. Such an insertion is now refused ("decided by the watchdog or
  the owner, not by a module or a plugin"), and the message waits in the queue for the end of the turn. Nor can they
  set the watchdog's urgency estimate shown on a queued message (`message.weighed`). The watchdog, the page's "now" and
  the owner's word through a module handle insert as before.
- In the Clean look the "specs" mark that stands in the status line of a session with /spec on (between the executors
  and the model) took room on the bottom row of the input field that the row's measure did not count, and was drawn
  under the field, so it could not be seen: on a narrow field, and the more at a larger "Text size", the executors
  button ran under "+". The mark is now counted in the room and shown in its place; sessions without /spec on look as
  before. Reload the page (⌘R / Ctrl+R); no restart needed.
- The hub page, "Schedule": a job is one row — time, title and cadence; the text, the command, the next runs and
  "Remove" unfold on a click. The title is the first phrase of the job's text, cut at a word (a command job is titled
  by its id). Counters, ids and "set by" are gone from the row, "Show reply waits" is the last line, and the clock pill
  shows only when the clock is stopped. The row of session chips is now one "Session" field with search: type a name,
  walk the list with Tab or the arrows, pick with Enter or a click, clear it with the cross. The "All sessions" button
  and the other back buttons are a "‹" sign, and the titles of Schedule, Settings, Updates and Hubs share one font.
  Reload the page (⌘R / Ctrl+R) after the update; no restart needed. — thanks: @visualpharm
- Page: the hub's feed no longer shows the core's service tag in front of an executor's result ("REPLIED d12:", also in
  the translated lines and in "REPLIED d12 (continuation):"): the line starts with who replied to whom. In Classic and
  the plugin themes not built on Clean, the grey line under a message (time, status) is the regular small text size
  instead of the smallest, and the "All" and "Now" switches above the session list show the word alone, the number of
  sessions is in their tooltip; Clean and the themes on it are as before. Nothing to do for the update; no restart
  needed.
  — thanks: @visualpharm
- Module API 2.10: a core module with the `message.rush` right sends an urgent message (`now`), and the core types it into
  a busy session in the middle of its turn, as the hub's `hub send --now` (see EXTENDING.md, "Urgent messages"). A
  module without the right, or a plugin, is refused; an urgent task from a module is refused as before. The module list
  names the right ("send urgent messages"). For modules that relay outside events which should not wait for a long turn
  to end. Restart: `hub restart` for the core to load it. —
  thanks: @visualpharm
- Page, "Updates": the screen is short. The pill beside the title is the version alone (the commit is in "About the
  hub"), the paragraph about how updates work is gone (the check line with its button says the same), a release is one
  line with a readable date ("November 13") and only the newest one is open; a note is shown whole and wraps. A past
  update is a line too, with an icon of its outcome, no "Update history" heading above; one with no notes and no reason
  has no fold sign, as there is nothing to open; only the latest one, when it failed or went back, is open, and only its
  reason is in the warning color — and not after a later check has said the hub is on the latest version: a failure the
  hub has got past reads like any past one, also on a page that stayed open. What you opened or closed yourself stays
  so; what the page opened itself follows the state. A version on trial is said once, in the check line; so is one
  waiting for a restart while the check line says it — after a later check has said "latest version" and the hub still
  runs the old code, the heading says it, as before. When a check or an install fails, the check line says why. The fold
  sign is an icon (in the Clean theme — the theme's own). "What is new" in Settings → "About the hub" is as it was:
  every day in full. On a phone there is room under the last line, so a plugin's floating button does not cover it; in
  the Clean theme the title is in the display font.
  — thanks: @visualpharm
- Page, Flow: the bar is the title and the period. The "signals: N" counter, the line about what the window shows, the
  second "now" beside the period list and the legend under the canvas are gone: what a line carries ("message",
  "assignment", "turn result") is written along the line itself, in the language of the canvas; a line shorter than the
  word carries none, and the word never covers a dot. "N more not shown" stays only when the canvas draws fewer sessions
  than there are. The drawing fills the canvas (a margin of about 48px), "You" and "Hub" stay in the center. Every dot
  with a signal has its name — also on a big hub, where a name moves beside its dot if the place under it is taken (on a
  phone-wide canvas a name is always below its dot, so it is clear whose it is). "Now": a session that is not at work
  and had no signal in the last 15 minutes is a quiet dot — an empty ring of a dot's size, named when you point at it or
  touch it — so the ones that talk stand out and a quiet hub is not an empty canvas (on a big hub sleeping silent ones
  are not drawn, as before). A session at work or with a question breathes and is named, as before. A replay of a period
  draws as before. A lead's folded executors are a dashed ring around its dot and their number after its name ("lead
  +3"): a press on the dot opens them, the next press opens the session, a press past the dots folds them back. Opened
  executors stand apart by the length of their names and stay on the canvas, also around a lead next to its edge; they
  stand in rows by their lead, in their order, each name under its dot; a name lies on no dot, and with many executors
  the names are cut shorter to keep them near the lead; on a phone every one of them is named — long names are cut in
  the middle to the room they have, the whole name is in the tip. Names on the canvas are 13px on a pad of the page
  color, so a line never crosses the letters; a dot's tip has no "sent 0 · received 0"; the system session of the
  liaison is named as in the list. Both are static files: reload the page, no `hub restart` —
  thanks: @visualpharm
- Voice: a client can send a recording as the raw sound instead of base64 in JSON — `POST /api/voice/raw?mime=…&session=…`
  (the whole recording in one request), or in chunks of one upload session: the first request with `done=0` answers
  `{id, size}`, the next ones add `id=…&offset=<size so far>` (a repeated chunk adds nothing, a gap is answered 409 with
  the size to continue from), the last one without `done=0` answers the text as `/api/voice` does. The hub tells such
  clients by `voice.raw` in `features` (only while voice is on). The base64 route stays for older clients. A chunk
  that arrives twice never doubles the sound, at most 16 sessions stay open (the oldest go first), a last chunk
  repeated while it is transcribed gets the same answer, session files are readable by the owner only. Needs
  `hub restart`. — thanks: @visualpharm
- After a start that moved a big journal to the archive, the hub knows again how long it was down. It took the time of
  that move for the moment it went down, so a hub off for days counted a second or two: the Telegram line "the hub is
  back, it was not there from … to …" stayed silent and the stats counted no downtime; this part needs `hub restart`.
  `hub off` also says what brings the hub back by itself, right after the update: nothing runs while it is off,
  schedule jobs included, until `hub on`, the next sign-in (macOS; Linux without linger) or the next boot (Linux with
  linger, which the one-line install asks for); on Windows only `hub on`.
  — thanks: @ManyLattice
- A start right after a crash of the core is seen as one again when that start moves a big journal to the archive.
  Before, the sessions whose turn the crash cut were told "a hub restart cut off your turn" instead of "the hub stopped
  without a clean shutdown", as after an ordinary restart. Needs `hub restart`.
  — thanks: @ManyLattice
- Page: `/api/file` answers with an ETag, and a repeat request with `If-None-Match` gets a 304, so a client does not
  download the same picture again. This changes every hub, not only clients that ask for a thumbnail: the answer's
  `Cache-Control` is now `private, no-cache` instead of `no-store`, so the browser or the phone may keep the owner's
  files on its disk and revalidate them by the tag each time (a shared cache must not keep them); "Sign out" on a hub
  with a page password clears that browser's copies (over https and on this computer; browsers ignore it over plain
  http). `/api/file?path=…&w=<px>` gives a raster picture scaled by width (a jpeg from the Files module's cache; the
  width is rounded up to 120, 240, 480, 960, 1920 or 2048). Only on a Mac with `sips`; elsewhere, for a picture that is
  not wider, is over 64 Mpx or is not really a picture, and when both scalers stay busy for 2 s, the original is served.
  Clients see `file.thumb` in the snapshot's `features`. Plugin API version unchanged (1.36, not released yet). —
  thanks: @visualpharm
- Accounts: a token command that hands out a different account on every call (a gateway) no longer makes the hub
  flicker at the end of the week between "limit economy is over" and "none free, reset <date>", each time with another
  date. While all accounts are busy, a fresh token is taken at most every 30 minutes instead of 15, and the economy mode
  holds until the fresh token's own limits are known. If no session reads them for 30 minutes, queued work comes up and
  reads them: at the limit the hub goes back to the economy without a new "none free", with reserve it hears "free
  again". "None free" is said once per episode and again only for an earlier reset, not for a token command that failed
  or recovered. "Continue" on a session cut by the limit works on a fresh token nobody has read yet instead of waiting
  for the earliest reset. Needs `hub restart`. — thanks: @Eugenius-S
- Voice: a recording in a plugin's field (a plugin that records through `hub.input().record()`) no longer turns the
  session field's microphone into a second red stop next to the plugin's own; the session microphone is off until that
  recording ends, as the dictation button. When the plugin's field leaves the screen mid-recording (its panel closed or
  removed), and when you move to another session while one records, the session field shows the stop, so the running
  microphone can always be stopped. Reload the page (⌘R / Ctrl+R) after the update. — thanks: @visualpharm
- The hub page left open on a long feed no longer keeps the computer busy. On every snapshot (once a second while a
  session works) the page put the whole feed in anew, recomputed the styles of the whole page from the input box, rebuilt
  the status line under it and highlighted all the code again; the Clean look also swept the whole page on every tick of
  a working session's stopwatch. On a feed of a few thousand rows the tab took a whole processor core and froze for up
  to a couple of seconds at a time. Now the rows that did not change stay as they are, and at rest Clean does nothing.
  The look and the scrolling are the same. Reload the page (⌘R / Ctrl+R) after the update; no restart needed.
- The session list no longer rewrites its rows on every snapshot in the Clean look: the page kept erasing the marks Clean
  puts on the rows and Clean put them back (thousands of changes a minute), and the field under the feed swapped its long
  and short hint the same way. A click on "More", on a step or on "Show earlier" now takes a text selection in the feed
  off, as it did before the feed began keeping its rows: while text stays selected the page holds the news back, and it
  looked as if it stood still. A reply's avatar follows a new icon of the session. Nothing changes in the look. Reload the
  page (⌘R / Ctrl+R) after the update; no restart needed.
- "Waiting for you": a result that lists an action you can take only later (the job that makes the file still runs,
  "it ends at 16:30, then you take the file") no longer marks the session as waiting for you. The same action is marked
  once the result says it is possible. Sessions now write such an action with its condition and time, apart from what
  they need from you now. Needs `hub restart` for the mark; running sessions get the rule with their next message.
  — thanks: @visualpharm
- Modules: a module command run from a schedule job's command or check (`hub schedule add --command`) now tells the
  module which job called it (`caller.schedule_job`). A command job runs as the owner without a session, so a module
  that keeps something for a session — like a to-do item whose reply goes to the session that made it — can find the
  job's session instead of keeping it for nobody. Nothing to do; no restart needed (the next command reads it).
  — thanks: @visualpharm
- A `Content-Length` (or a module answer's length or status, or a download's `Retry-After`) written with digits that
  are not ASCII (`²`, `١٢`) no longer drops the connection: `POST /api/upload/raw` and the module proxy answer 400 for
  such a request length, and a module answer's own length of that kind is taken as no length. Needs `hub restart web`.
- A second hub made by `hub instance new` from a git hub can now move onto the app with the one-line install
  (`--instance <name> --move`). Its code clone lies in `<state>/code`, where the app puts its own code, so the move
  used to answer "not the clone this hub runs from". Now the move takes that clone out to a folder beside the state
  folder (`~/.hub-<name>` gives `~/hub-<name>-clone`, a taken name gets -2, -3…), puts its linked git worktrees right
  again (`git worktree repair`), and `<name> app undo` brings it back to `<state>/code` and the hub on it. The hooks and
  the Codex settings of the sessions are written again at the hub's start, so they name the new code by themselves.
- Settings → Look: the hint for your own theme files names the folder this hub reads (`themes/` in its settings
  folder: a second hub's own one), not always `~/.config/hub-core/themes/` of the main hub. `GET /api/themes` has the
  folder as `folder` (Plugin API 1.36). Needs `hub restart web`; reload the page (⌘R / Ctrl+R) after.
- For clients (Plugin API 1.36): when switching to an account is refused (`POST /api/accounts/use`, answer 409), the
  answer has `code` (`paused` or `busy`) to tell the refusal by, and for a busy account `why`, the cause alone in the
  hub's language: the limit the account waits for, "5-hour limit spent" or "weekly limit spent" when it is used up,
  otherwise its share or the reason as before. So a client can show "busy until 15:17 — 5-hour limit spent"; `until`
  and `forceable` are as before. `error` and `hub accounts use` name the same limit: before, with the 5-hour and the
  weekly limit both used up, they said "5 h limit 100%" next to the week's reset. Needs `hub restart web`.
  — thanks: @visualpharm
- Page: a plugin view's ☰ menu item shows the number the plugin passes in `addView({ badge })` (a positive whole
  number, plain text at the right; 0 or anything else shows nothing). Re-registering the view with a new number
  updates it. Plugin API 1.36 (`hub.capabilities.viewBadge`). Reload the page (⌘R / Ctrl+R) after the update.
  — thanks: @visualpharm
- On a phone, the feed search field no longer shrinks to ~130 px: the match count ("none", "3 of 12") takes only its
  own text instead of a reserved slot, so the field gets the rest of the line (about 200 px on a 390 px screen, in every
  theme). The field is a little narrower while the count is long ("123 of 456"). Reload the page (⌘R / Ctrl+R) after
  the update. — thanks: @visualpharm
- A session's browser no longer stays dead when chrome-devtools-mcp keeps one failed page. On a machine deep in swap
  its page setup could time out ("... timed out. Increase the 'protocolTimeout' ..."); puppeteer then keeps that
  failure for the tab, so every later call failed the same way in 0 s until `hub session reload`, though Chrome itself
  answered again. Now the proxy takes such an error that comes back at once (a real timeout takes minutes) for a stuck
  process and replaces it before the next call: the session sees the slow error, maybe one fast repeat, then the call
  goes to a new process. After two replacements in a row it gets "reconnect the browser" with the repeat in 60 s, as
  for a hung connection; a call after that pause starts a new series of replacements. Only sessions with a local
  browser that starts on demand or on login; nothing to turn on, it applies to sessions started after the update. —
  thanks: @ManyLattice
- Page: each open question card has a "reply" button in its head. It aims the session's message box at that card: the
  card is highlighted, the bar over the box names it, and the words you type or dictate go as the comment on that card
  instead of a plain message. On such a card a tap on an option only marks it; the answer is the option plus the
  comment. With several questions in a card the comment answers the ones left without a pick; a file in the box goes as
  its path in the answer. A card closed in the meantime (answered in Telegram, say) or a refused answer says so and the
  words stay in the box: they never go as a plain message by themselves. No restart is needed: reload the page.
  — thanks: @visualpharm
- Voice messages on the page: while one records, a screenshot can be added with "+" (the paperclip), ⌘V / Ctrl+V or
  a drop. The recording goes on, and when it stops the words and the files go out as one message, with the "[Image #N]"
  marks kept. A file still uploading holds the words in the box until it is done; a failed upload leaves them there
  next to the error. While the file picker is open, the recording does not stop on silence. Reload the page
  (⌘R / Ctrl+R) after the update. — thanks: @visualpharm
- The page and the apps on a slow or breaking connection: an update of the state stream now carries only the items
  that changed. One session that started working used to resend the whole session list, one new message all the
  messages, one schedule job the whole schedule; on a copy of a busy hub's journal the updates for 600 events took
  14.5 MB (3.2 MB compressed) and now take 1.2 MB (0.17 MB). The hub page uses it after `hub restart web` and a page
  reload. For other clients (Plugin API 1.36) it is opt-in: open `/api/stream?delta=2` when `features` has
  `stream.delta2` (the format is in EXTENDING.md, "/api/stream?delta=2"); without the parameter, and for the apps of
  earlier versions, the stream is as it was — thanks: @visualpharm
- For clients (Plugin API 1.36): the event stream (`/api/stream?feed=inbox`) carries a plugin channel's new posts as
  `feed` events, so an app no longer polls `/api/feed` for them; `features` has `stream.feed`, the format is in
  EXTENDING.md. A stream opened without the parameter is unchanged. Needs `hub restart web` — thanks: @visualpharm
- A hub made by `hub instance new` from a hub whose code has no `origin` (a core developer's clone) used to stay without
  updates in silence. Now the command says so in its plan (also with `--dry-run`) and again when the hub is up, with the
  way out (`git -C <code> remote add origin URL`, then `update.enabled` in hub.json; or `--origin URL` at
  once), and "About the hub" shows "this hub's code has no update address" instead of "turned off in the settings".
  Nothing changes for a hub with an origin. Reload the page (⌘R / Ctrl+R) and `hub restart update` after the update.
- An MCP server that did not come up in a session is no longer hidden behind its granted tool: `hub status` adds a line
  under a live session — "MCP did not come up: browser:agents — hub mcp reconnect browser:agents --session <name>, or
  hub session reload <name>". It covers Claude and Codex sessions alike.
- A browser whose MCP program is missing says so. When `browser.mcp` in hub.json names a program that is not on the
  PATH (for example `chrome-devtools-mcp`, never installed), a session's browser tool used to fail with "connection
  closed" while the browser showed as working. Now the browser's row in "Settings → Modules → Browsers for sessions"
  names the missing program and the setting to fix, and the session's MCP launcher stops at once with the same words
  instead of a traceback. Needs `hub restart browser` for the row; reload the page.
- Clean on a wide screen: the feed and the input box stay one centered column (1104 px, the width at 1440 with the
  list open) instead of stretching over the whole window — on a 2560 screen the input box was 2224 px wide and the
  messages stuck to the left edge. Up to 1440 nothing changes; Settings, Schedule and Updates in Clean stand in the same
  column on a wide screen. Classic keeps its 800. Reload the page (⌘R / Ctrl+R) after the update.
  — thanks: @kalimullin
- Clean: the strips under the All/Now tabs ("away", the paused executor queue, "no connection" on a phone) no longer
  stick to the tabs: there is an 8 px gap while a strip shows, and the list does not move when there is none. — thanks:
  @ManyLattice
- Clean: a new option "State counts under the list" (Settings → Look, off by default). On, the line under the session
  list shows how many sessions are waiting for you, working, free, failed and asleep, each with a dot in the theme's
  colors, as its own row above ☰ and the limits. Off, the footer is as it was. Reload the page (⌘R / Ctrl+R) after
  the update. — thanks: @ManyLattice
- The first message of an executor (and of any task a session sets) shows only the words of whoever set it. What the
  core adds for the model — the report rules, the task record line, the language line — is folded under "for the model
  only" and opens on a click with the full text. Tasks set before the update look as they did. Reload the page
  (⌘R / Ctrl+R) after the update.
- External hubs: when you ask the hub in Telegram to ask another hub something, the answer that comes back now reaches
  you in Telegram as a reply to your message, as any other answer to it does; before, it stayed on the page. Words from
  another hub that answer no request of yours still stay on the page. Needs `hub restart`.
- For clients (Plugin API 1.36): `GET /api/about?lite=1` is the "About the hub" card without the notes of past updates,
  the CHANGES.md entries and the list of unpublished commits (the full card reaches 1.5 MB), with an `ETag`
  (`If-None-Match` → 304); the flag `about.lite` in `features`. `GET /api/about` without the parameter answers as
  before. Needs `hub restart web`. — thanks: @visualpharm
- The core's reminders "task is open and you are free" to one session now come at least 10 minutes apart, instead of a
  pile when many of its tasks went quiet at the same time. The reminder also says that "—" is enough when there is
  nothing to add. Nothing to enable; needs `hub restart` — thanks: @Eugenius-S
- Messages: the owner's words from the page or an app to a session that is gone (removed, closed) or never was (a
  client's stale list, a mistyped name) are no longer refused with "no session" (the apps showed a to-do with Retry) or
  left undelivered: they go to the hub with the addressee named (the words to a removed executor, to whoever raised it,
  with its last result), and the hub passes them on. Their feed row carries `addressed`, the name he wrote to, so a
  client can show "to X, passed on". Sessions still get the refusal; `hub send` in the terminal still answers "no such
  session" with the closest names. Needs `hub restart` — thanks: @visualpharm
- Clean: a new option "Usage bars" (Settings → Look, off by default). On, the usage at the bottom of the session list is
  two bars, "5 hours" and "week", instead of the "5 h 40% · week 22%" line. Hover or tap a bar for its percent; the
  switch next to them shows a row per window with when it resets (the weekly Opus and Sonnet windows too, when the
  hub has them), and the account e-mail or name; with accounts on, a button next to the bars opens Settings →
  Accounts. Grey below 80%, orange to 95%, red from 95% or when the account does not work; yellow in limit saving. An
  old reading is paler, with a clock that says why. Off, the line stays as it was. Reload the page (⌘R / Ctrl+R)
  after the update. — thanks: @visualpharm
- Settings → "About the hub" in the app: the page no longer slides down by itself in jumps when it is scrolled a little;
  the block with the browser and the hub check is now filled before it is put on the page, so it does not grow above
  the place you read after every redraw.
- Hubs from the package on macOS download their own Chrome for snapshots (`hub shot`, diagrams) even when Google
  Chrome is installed; before, snapshots failed there. A hub that is already installed fetches it by itself after the
  update. Where that is not possible, `hub shot` names the install command for this hub's settings folder (not a fixed
  one), and a chrome-headless-shell kept there is found.
- The hub page: an open session no longer converts every message's markdown again on each update of the feed (the
  feed is rebuilt on every update and each message was sanitized anew, with a fresh document per message). The
  converted text is kept per message, so a redraw of an unchanged feed converts nothing. Static files only: reload the
  page, no `hub restart`. — thanks: @visualpharm
- A session that already leads a direction can no longer take over a second one with `hub direction new`: the old
  direction kept losing its lead; the hub starts it with its own lead (`--session`). Needs `hub restart`
- Claude accounts: an account whose token comes from a command (conduit, for example) no longer stops the sessions
  "until the reset" when a fresh token runs out within 15 minutes of the previous one. If that token was well below
  the switch threshold at its first reading and was then used up, the core asks the command for another one at once,
  and the turns cut by the limit continue on it. Before, the core said that no account was free and asked the command
  again only after 15 minutes. A token that arrives spent or close to the limit still waits 15 minutes for the next
  one, so a command that keeps handing out such tokens does not restart the sessions more often than before. Needs
  `hub restart` — thanks: @GreatMedivack
- Claude accounts: a new choice mode, `hub accounts prefer reset` (off by default). Among the accounts with reserve
  (not busy, windows below the threshold with a margin, no sign-in refusal) the pool uses the one whose weekly window
  resets first, so the reserve that would be lost at that reset is spent first, with no manual reordering. It moves only
  when that reset is at least an hour earlier than the active account's; when that account's 5-hour window fills, the
  pool moves on and comes back once that window resets (`hub accounts wait <min>` waits for a near reset instead). An
  account whose reset is not known goes after the known ones, in pool order. A hand-picked account (`hub accounts use`)
  stays as before. `hub accounts prefer on` (back to the first in order) and `off` work as before; one mode at a time.
  Needs `hub restart` — thanks: @visualpharm
- "Waits for you": a result that only reports a blocker the session handles itself — "the computer is locked, so the
  message can't go now; it goes out at 08:15", a retry is queued — no longer becomes an owner to-do item such as
  "unlock the computer and send". A direct request to the owner ("unlock it before 08:15") still does, and so does a
  blocker with no retry or schedule of the session's own ("locked, the message did not go out"). Takes effect after
  `hub restart`. — thanks: @visualpharm
- Tasks: when a session still has a task open from an earlier turn (that turn ended with no text) and its next turn
  is started by the core's note that one of its executors handed in ("record the lessons"), the reply to that note no
  longer counts as the task's result: the task stays open, whoever set it gets no REPLIED with "Already recorded",
  and the owner gets no such answer to their message. The session's next real turn answers the task as before.
  Needs `hub restart`. — thanks: @visualpharm
- `hub send`, `hub task run` and `hub task note` with `--file -` now read the text from stdin, like a bare `-`,
  instead of failing with "cannot read -". No restart needed: `hub` reads `bin/hub` directly — thanks: @visualpharm
- The hub's own Claude Code: every release names the Claude Code version it was tested on, and the hub downloads exactly
  that version by itself, without a model and without asking, into its own folder (`claude-code/<version>/` in the hub's
  state folder, about 300 MB; Anthropic's signed manifest and sha256), then runs its sessions, the critic and "Setup" on
  it. The `claude` in your terminal (`~/.local/bin`, npm, Homebrew) is no longer installed, updated or touched by the
  hub, and which one you have no longer matters. The download comes before an update goes in; a hub already on this
  release downloads it at its next scheduled update (on a Mac, not while it sleeps) and its next sessions start on it,
  no restart. If the download fails, the update waits, the owner gets one line with the reason, and the hub tries again
  a day later (`hub update` tries at once). Three versions stay: the running one, the previous one for a rollback, the
  one being prepared. A hub update also used to stand still when the new version needed a newer Claude Code: it no
  longer does. A hub with updates off keeps running the `claude` it finds in PATH. Nothing to do by hand; a hub without
  a Claude Code sign-in signs in on its page ("Setup"). `hub setup claude` now takes no channel or version, only
  `--force`.
- `hub` commands that sessions typed in a form of their own now either work or say exactly what is wrong (a week of
  sessions: about 170 refusals by parameters in about 100 sessions, each a wasted step). `hub critic --evidence` takes
  the paths separated by spaces as well as by commas, and `~` in every path; `hub session wake` takes the text from
  `--file` or stdin like `hub send`; a mini-hub's word to its executor asleep after handing in, without `--about`, goes
  as the rework of its one task from that mini-hub. A refusal names the wrong word, the closest right one, the common
  mistake from the command's help, its form and an example, instead of the whole help; the backlog of a direction
  answers `--help` after any words and `doc create` takes `--content`. The critic's rule in the sessions' prompt shows
  the help's form and example. Needs `hub restart` for the core's refusal texts and the critic's rule.
- The hub's web server spends less CPU on each update of the page: building the state it sends to open pages looked up
  the text language and the translation catalogs again for each of its thousands of texts, and grouped the active and
  paused scheduled jobs by cutting and joining every job's text for each comparison. Now the language is looked up once
  a second and each job's text is read once. On a hub with about 5 thousand sessions one build takes 0.035 s of CPU
  instead of 0.053 s, with the same result. Needs `hub restart` — thanks: @visualpharm
- The hub page: a screen recording from the iPhone or Mac app (`screencast-<id>.mp4` with its `.events.jsonl` pointer
  track) shows in your message as a player with "Screen recording · 0:05" under it, as in the phone and Mac apps,
  instead of the note the app writes for the session and two file links. Other attachments stay; the length comes from
  the app's note or, without one, from the video itself. The message the session received is unchanged. A page reload
  is enough, no `hub restart` — thanks: @visualpharm
- With the account gateway on, a schedule job's command or check and a `hub run` now go through it like a session: their
  `claude -p` used the machine's own sign-in and ran into its limit while the account pool stood idle. The gateway
  off, a cloud provider or an API key of the job's own set — nothing changes. A sign-in module's own gateway is not covered yet. Needs `hub restart`.
- Limits: a usage share no longer goes down inside the same window. Readings come out of order from many sessions, so
  at the threshold a late 94% after 95% lifted the account pool's "busy" mark seconds after a move off the account, and
  flipped the economy mode; the limits line showed the share going back. A window a reading lacks keeps its last value
  until its reset; a new reset, a new token or an early reset (a drop of 5% or more) are taken as they come. Needs `hub
  restart`. — thanks: @ManyLattice
- Limits: the weekly window that includes overage ("week incl. overage", sent by the account gateway) now counts in the
  limit economy like the 5 h and weekly windows, and a refusal on it holds the account full until its reset (before, a
  single login refused on it stayed in the normal mode). When a Claude account starts to be billed as overage (extra
  usage beyond the plan, charged separately), the hub hears it once and tells the owner. A hub that never goes into
  overage sees no change. Needs `hub restart`. — thanks: @ManyLattice
- The hub page: "Reply" now stands on your own messages and on your answer to a question card ("Answer: …"), not only on a
  session's lines. The quote is your text (for a card — what you chose); the reply goes to the session of that feed, and a
  fragment of your own message can be selected and quoted too. A tap on the quote of such a reply jumps back to your own
  line, not to a session's longer line that repeats your words. In Clean the button stands outside your bubble, on its
  start side; in the classic theme it is a quiet link on hover (always shown on a touch screen). A page reload is enough,
  no `hub restart` — thanks: @visualpharm
- For the hub's own clients: `ref` in `POST /api/send` — which reference or note of the client the owner answered
  with words from its page (a line of up to 128 characters), so that the reply and the session's answer to it
  continue there as a thread, the same way as `todo` for a to-do item. The hub writes it to the journal together
  with the message (`message.received`) as is and does nothing with it — the module that keeps the references reads
  it. A client that does not send it works as before. Nothing to do; the page takes it after `hub restart`
  — thanks: @visualpharm
- Cards to the owner: a session now puts "later" ("tomorrow", a time, the window) first only with a reason it checked
  just now (a backup or a gate that blocks the start at this moment, someone else's stated limit, a fresh
  measurement), not a guess about load or people, and asks someone else's date "can it be sooner?" first. The owner's
  earlier "yes" for a time ("tonight") no longer carries to a new time: the first option is the nearest moment the
  checked facts allow. Nothing to enable; it applies to sessions started after the update (`hub restart`).
- Clean theme: the limits line at the bottom of the sidebar (the default view, without "Usage bars") now turns yellow
  while limit saving is on, as the usage bars and the native page do; it read a snapshot key the core never sends.
  Nothing to do; ⌘R on the page.
- Page, Clean theme: in a narrow column the Settings sections ("About", "Look", "Usage", …) no longer vanish. When the
  bar does not fit the header, a ☰ button after the title opens the list of sections (a pick switches the section and
  closes the list; Enter, ArrowUp/ArrowDown, Home/End, Tab and Esc work from the keyboard). The bar is measured against
  the real width, so a language with longer names folds earlier. A wide column is unchanged. Reload the page (⌘R).
- Clean: the executors counter ("29 ›") in the session list stands on the line of the name again at the larger text
  sizes; it used to sit a few pixels lower. Nothing to do; reload the page (no restart needed).
- Codex is no longer labelled an experiment: "Add a hub" says only that the sessions' texts go to OpenAI, `hub codex`
  help and the repository description name Codex as the second engine next to Claude Code. Nothing to do.

### For sessions
- What you need from the owner, in the gist and in the result, is only what they can act on at once: an action that
  becomes possible later (a build still runs, a delivery is on its way) goes with its condition and time — "after the
  build (~16:30): pick up the file" — not as needed now. — thanks: @visualpharm
