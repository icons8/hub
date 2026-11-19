# The hub bus relay

A small server that carries the hub bus's envelopes between hubs that cannot reach each other directly (a laptop
behind NAT, a hub on another network). One file, `server.py`: the Python 3.12+ standard library and `ssh-keygen`; with
the cryptography library installed (the image has it) it signs and checks in its own process, the same signatures
without two runs of `ssh-keygen` a request. Anyone can run one; a team usually runs one for everybody.

Hubs never trust the relay with a text: an envelope is sealed by the sending hub's core for the receiving hub's key
and signed by the sender before it leaves the hub. The relay keeps bytes until their expiry and checks only their outer
form.

## What the operator sees and can do

Sees: which hubs are registered (their fingerprints and the labels they gave), who invited whom, which hub writes to
which and when, the size of each envelope (rounded up to a bucket: 1, 2, 4 … 32 KiB, then 48, 64, 80 KiB), when a hub
asks for its mail, the cards of introductions (public by design: keys, label, the relay's address, the sessions a hub
shows). The relay does not store or log IP addresses; whatever stands in front of it (a proxy, a CDN, the host) may.

Does not see: the text, the sessions' names, threads, receipts — they are inside the sealed box; a receipt and a message
of the same bucket look the same.

Can: refuse, drop or delay an envelope until it expires (the sending hub sees it as not delivered), remove a hub, count
activity. Cannot: read or change an envelope, write as a hub, replay a hub's request (each is signed for this relay,
with a time and a one-time nonce).

## Registration: by invitation only

The operator is needed once for the whole relay — for the first hub:

```
python3 server.py bootstrap --url https://relay.example    # the first hub's invitation (or HUB_RELAY_URL)
```

It prints one line, `hub-relay-invite-1:…` (what to do with it goes to stderr), and checks that a hub will take the
address (https; plain http only to the relay's own machine). Asked again before the first hub came (the output was
lost) it prints the same line, not a second one; once a hub is registered it refuses — from then on a new hub comes with
a link from a hub already here. `server.py invite` makes an operator's invitation at any time (a hub lost, another first
hub).

### A relay open to everyone

```
python3 server.py open [--days 365] [--url https://relay.example]   # one line for everyone (asked again, same address — the same)
python3 server.py open --off                                        # the line lets no one in; its hubs stay
```

The open line is an operator's invitation in the same format that any number of hubs register by, for its term (a year
by default). Put it where the hubs' owners take it. It has its own caps: 500 hubs a day for the whole relay
(`HUB_RELAY_OPEN_DAY`) and 5 a day from one address (`HUB_RELAY_OPEN_ADDR_DAY`; an IPv6 address counts by its /48 — a
site; kept in memory, never written); over them a join is refused `joins_open` / `joins_addr` and the hub tries later.
The cap per address needs the client's address: behind a proxy set `HUB_RELAY_TRUST_PROXY=1` (below), otherwise every
hub is the proxy's one address. The daily cap of joins by members' links (below) stays: with both, a relay takes at most
`HUB_RELAY_OPEN_DAY` + `HUB_RELAY_JOINS_DAY` new hubs a day. A hub that came by the open line is like any other:
it reaches the hubs it links with, and takes boxes only from the hubs it named. `open --off` closes registration by the
line (a mark of it stays, so the same text is not taken again, also by the code before this); the members' links still
register the hubs they are given to.

The owner of the first hub pastes the line where a link is pasted (the page's Settings → Hubs) or gives it to
`hub bus relay join`. The address and the relay's key come from the invitation (the hub has no relay address of its
own) and go into the hub's card, so its contacts know where it is. After that the hub makes its own invitations —
signed by its key, one-time, valid from ten minutes to seven days; a link that joins two hubs carries one. Per hub: at most 5 unused
at once, and the invitations used in the last 30 days (counted by the time of use) together with the unused live ones
at most 20 — a live invitation is a reserved place, so joins at once never go past 20; one that expired unused or was
withdrawn spends nothing. A hub withdraws its own unused invitation (`POST /v1/invite/revoke`, a withdrawn link) — the
place is free again and the invitation lets no one in; a mark of it stays until its term, so the same signed text is
not taken again. Invitations that expired unused are dropped a day after their term. A used one is not withdrawn: it is a member now, and only the
operator removes a member. At most 50 joins a day on members' invitations for the whole relay.

```
python3 server.py hubs                    # fingerprint, label, invited by, joined, queue
python3 server.py invites                 # who made each invitation, used or not
python3 server.py remove <fingerprint>    # requests refused, its queue and unused invitations dropped
python3 server.py remove <fingerprint> --tree   # and every hub it invited, and theirs
```

## Limits

Envelope ≤ 128 KiB (an introduction ≤ 8 KiB), kept until its own expiry (≤ 7 days); one copy per (receiver, sender,
id) — a repeat is "already", other bytes under the same id are refused; a queue ≤ 2000 envelopes and 256 MiB per
receiver, ≤ 500 and 64 MiB per sender in it (beyond — 503, the hub tries later); introductions have their own room — ≤ 500
waiting for a hub, ≤ 10 of them from one sender (fifty senders fill it, not ten) — and never take the boxes' place;
a sender introduces itself to at most 100 hubs a day (`intros_day`); a receiver takes envelopes only from the hubs it
named (`/v1/allow`), introductions from any registered hub. A registered hub's signed requests: 300 at once, refilled
120 a minute (`HUB_RELAY_HUB_BURST`, `HUB_RELAY_HUB_MINUTE`; over — 429 `rate`, before its signature is checked),
spent after the signature checks and only by a new nonce, so requests in another hub's name or repeated ones spend
nothing of it; 30 failed or unsigned requests a minute per address — an IPv6 address by its /64. 64 long polls at once
(`HUB_RELAY_POLLS`), 2 per hub. A letter wakes only its receiver's long poll.

Connections at once (each holds a thread): 1024 for the whole relay, never fewer than two for every long poll
(`2 × HUB_RELAY_POLLS`), and 64 from one address (an IPv6 address by its /64; the loopback and the private networks
10/8, 172.16/12, 192.168/16 are not counted as one — a proxy in front without `HUB_RELAY_TRUST_PROXY` or a stand is
every client at once). A process whose file limit is short raises it up to its hard limit, or takes fewer
connections and says so on stderr. Over them a connection is refused at once
with an unsigned 503 `connections` / `connections_addr` and nothing of it is read; the hub reads it as «try later».
Behind a trusted proxy every connection is the proxy's: the address's cap counts the requests under way of the address
the proxy names. Reading one request — its line, headers and body — has 30 seconds in all (the socket's 15 seconds are
for one read), so a client sending a byte at a time does not keep its place; a long poll's waiting is not reading.

The database has a ceiling: 8 GiB of pages in use (`HUB_RELAY_DB_MAX_MB`, 8192 — under a volume of 10 GiB, with room for
its write-ahead log); free pages do not count, they take the next writes. Over it new letters, introductions,
registrations, invitations and longer allow lists are refused with 507 `full`; reading, acknowledging, leaving and
everything a hub asks keep working, and each acknowledged letter makes room. Invitations used more than 30 days ago are
dropped (the quota counts 30 days).

## Your own relay

A hub links through one relay. The public install's hubs use the public relay its pages name; a team that wants its
hubs apart runs its own — one command on a Linux server with Docker (and its Compose plugin), ports 80 and 443 free and
a DNS name pointing at it:

```
curl -fsSL https://<the pages of the release>/relay.sh | sh -s relay.example.com
sh relay/install.sh relay.example.com        # the same from a copy of this repository
```

It writes a compose project into `~/hub-relay` (`HUB_RELAY_DIR`): this relay, built by Docker from the release's own
tag, and Caddy in front of it with HTTPS of Let's Encrypt. It prints the first hub's line and keeps it in
`~/hub-relay/first-hub.txt`: paste it into that hub's "Accept an invitation" (or `hub bus join -`) before the hub links
with anyone; the next hubs come by its links. Run it again any time: the same project, and the same line until the
first hub came. By hand instead: run the image as below behind HTTPS, then
`python3 server.py bootstrap --url https://<its address>`.

## Running it

```
docker build -t hub-relay relay/
docker run -d -p 8080:8080 -v hub-relay:/data hub-relay
```

or without Docker: `HUB_RELAY_DB=/var/lib/hub-relay/relay.sqlite3 python3 relay/server.py serve` — on Linux or macOS
only: on Windows the relay has no file limit to raise (no `resource` module), and a slow client it cuts keeps its
connection's place until the socket's own timeout.

Settings: `HUB_RELAY_DB` (the SQLite file; the relay's key `relay.key` is made next to it on the first start — keep the
folder: a new key means new invitations for everyone), `HUB_RELAY_PORT` (8080), `HUB_RELAY_HOST` (0.0.0.0),
`HUB_RELAY_URL` (the public address for `invite`, `bootstrap` and `open`), `HUB_RELAY_TRUST_PROXY=1` (the per-address
limits read the proxy's `X-Forwarded-For`; without it — the connection's address), `HUB_RELAY_POLLS` (long polls at
once, 64), `HUB_RELAY_JOINS_DAY` (joins by members' invitations a day, 50; 0 — no cap), `HUB_RELAY_OPEN_DAY` (joins by
the open line a day, 500; 0 — no cap), `HUB_RELAY_OPEN_ADDR_DAY` (of them from one address, 5; 0 — no cap),
`HUB_RELAY_HUB_MINUTE` / `HUB_RELAY_HUB_BURST` (a hub's signed requests a minute / at once, 120 / 300),
`HUB_RELAY_DB_MAX_MB` (the database's ceiling, 8192), `HUB_RELAY_SIGNER=ssh-keygen` (sign and check with `ssh-keygen`
even if the library is installed). Put it behind HTTPS (a reverse proxy): a hub takes plain `http://` only to its own
machine, and `http.server` is not meant to face the internet alone.

Which address `HUB_RELAY_TRUST_PROXY=1` takes: the last element of `X-Forwarded-For` — the one the proxy right in front
of the relay wrote. That is the client when that proxy sets the header to the address it restored itself: Caddy (the
relay of `relay.sh`), or ingress-nginx with `enable-real-ip` (and, behind a CDN, `forwarded-for-header` naming the CDN's
client header) and without `compute-full-forwarded-for` — then it writes one address. With `compute-full-forwarded-for`
the last element is the hop before nginx (a CDN's edge): every client behind one edge would be one address. The relay
must be reachable only through that proxy (a client that reaches it directly writes the header itself). The check: the
31st unsigned request a minute from one address is refused 429, a request from another address in that minute is not.

The image runs as user 10001 (by number: a pod with `runAsNonRoot` takes it). SIGTERM stops the relay at once (the
database closed, its log folded in) — also as a container's PID 1, which has no default for it. In a namespace with a
Service named `hub-relay`, Kubernetes puts `HUB_RELAY_PORT=tcp://<ip>:<port>` into the pod (the docker links'
variables): that is the Service's port, and the relay keeps its own 8080. Its health check signs and checks with
the relay's key, not only "it answers", and fails when the cryptography library does not load — without it the relay
signs with `ssh-keygen` (two runs a request; `serve` says so on start): `HUB_RELAY_SIGNER=ssh-keygen` chooses that.

## Protocol

Every request but `GET /healthz` and `GET /v1/hello` carries `X-Hub-Fp`, `X-Hub-Time`, `X-Hub-Nonce` (32 hex) and
`X-Hub-Sig` (base64 of an `ssh-keygen -Y sign` signature, namespace `agents-hub-relay-v1`) over the lines
`agents-hub-relay-v1 request`, the relay's key, the method, the path with query, the time, the nonce, the SHA-256 of the
body. The time is good ± 5 minutes, the nonce once. The signing key is the hub's transport key, bound at `join` by the
hub's own key (`agents-hub-relay-v1 bind`, the relay's key, the fingerprint, the transport key). Every answer to such a
request carries `X-Relay-Sig`: the relay's key over `agents-hub-relay-v1 response`, the nonce, the status, the SHA-256
of the body.

A hub announces its bus version with `X-Hub-Bus-V: <N>` on every signed request (`/v1/join` included); a request
without it sets the hub's version to 1 — the version follows the requests, so a hub rolled back to an older core is
seen as version 1 from its next request on. The header is not in the signed line: a hub of a newer version talks to
a relay of older code as before, and only whoever stands behind the TLS (the operator) could change it on the way. It
is written in the same transaction as the request's nonce: a repeated request cannot set an old version.

| Method | Body | Answer |
|---|---|---|
| `POST /v1/join` | `{invite, sign_pub, enc_pub, transport_pub, bind, label}`; `invite` may be null for a hub already here | `{fp, invited_by, already?}` |
| `POST /v1/invite` | `{invite}` — the line, signed by the hub's key | `{id, exp}` |
| `POST /v1/invite/revoke` | `{id}` — an unused invitation of this hub | `{revoked: true}`; used — 409 `used`; none of this hub's — 404 `no_invite` |
| `GET /v1/invites` | — | `{invites: [{id, made, exp, used_by, used}]}` |
| `GET /v1/peer?fp=F` | — (registered hubs only) | `{fp, bus_v}`; not here, left or removed — 404 `unknown_to` |
| `POST /v1/allow` | `{from: [fingerprint…]}` (≤ 1000) | `{allowed}` |
| `POST /v1/send` | `{to, envelope}` (base64) | `{stored: "new" \| "already"}` |
| `GET /v1/inbox?wait=N` | — (N ≤ 25 s) | `{items: [{from, id, envelope}]}` |
| `POST /v1/ack` | `{items: [[from, id]…]}` (≤ 256; only one's own) | `{removed}` |
| `POST /v1/leave` | — | `{left}` |

`GET /v1/hello` (unsigned) answers `{v, key, bus_v}`: `bus_v` is the newest envelope version the relay takes (2; a
relay of older code has no such field and takes only version 1).

Refusals are JSON `{code, error?}`: `unsigned`, `bad_signature`, `clock`, `replay`, `unknown_hub`, `removed`,
`bad_invite`, `quota`, `joins_day`, `not_allowed`, `not_yours`, `unknown_to`, `conflict`, `bad_envelope`,
`busy`, `rate`, `bad_request`, `no_invite`, `used`, `connections`, `connections_addr`.

### The envelope's outer form

The relay reads only the outer form of an envelope `{head, sig}`: the head's set of fields must be exactly one of
these (a field more or less — `bad_envelope`), its id 32 hex, `ts < exp ≤ ts + 7 days`, not expired, not more than ten
minutes ahead.

| Form | Head fields |
|---|---|
| box v1 | `v: 1, t: "box", id, ts, exp, from, to, ct` |
| box v2 | the same with `v: 2` and `cls` ∈ `chat`, `line`, `liaison`, `ctl` (the receiving core counts by class before it opens the box) |
| intro v1 | `v: 1, t: "intro", id, ts, exp, card` |
| intro v2 | the same with `v: 2`, and at most one of: `link {id, sig}` (the hub came by a link: `sig` — the one-time key's signature, printable ASCII ≤ 1024), `link_ack {id, fpB, intro_id}` (the answer, bound to that hub and its introduction), `link_refused {id, fpB, code}` (`code` — lower-case letters and `_`) |

`ct` is base64 of a box of one of the size buckets; `card` names the hub's `sign_pub` and `enc_pub` (the relay
computes the sender's fingerprint from them); an introduction is at most 8 KiB. Both versions go through side by side:
a relay is updated first, then hubs send version 2 to contacts whose relay and core take it.
