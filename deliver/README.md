# The delivery server of the hub app

A small server that hands out the releases of the hub app's team build: the code layers
(`code-<version>-team.tar.gz`), the environment of each platform (the archive the one-line install downloads), the install
scripts, `releases.json` — what is current on each channel — and `min.json`, the minimum version the hubs must update to. One file, `server.py`, the Python 3.12+ standard library;
the files lie on a volume.

## Who may do what

| Request | Who |
|---|---|
| `GET /healthz`, `GET /min.json` | anyone: a hub below the minimum has to learn it even when its key no longer reads |
| `GET`/`HEAD` `/v1/releases.json`, `/v1/layer/<file>`, `/v1/app/<file>`, `/v1/script/<file>` | the build key: `Authorization: Bearer <key>`, or `?key=<key>` in the address (a link to an installer); `Range` continues a broken download |
| `GET`/`HEAD` `/i/<key>/install.sh`, `/i/<key>/install.ps1` | the build key, in the address (a pasted line has no header) — see The one-line install |
| `POST /v1/stand` | the build key (the release key too): "this hub stands on this release" — see Stands |
| `PUT /v1/...`, `PUT /min.json` | the release key only |

There is one build key for the whole team build: it is in the build and in the installer link the team shares. The
release key lives only on the machine that makes the releases (`tools/package/upload.py` of the core). The server
keeps neither: its settings hold their sha256. A key given in an address never reaches the log.

What a stolen build key gives: reading the releases, nothing else. A stolen release key gives a foreign layer to every
hub that presses "Update" after it — change it at once (`server.py keygen`, the new sum in the settings, the old one
out). Hubs trust the server by TLS and the sums in `releases.json`: that guards against damage on the way, not against
a broken-into server.

## Writing

- `PUT /v1/(layer|app|script)/<file>?offset=N&size=S&sha256=H` — the file's bytes from N. One body is at most 64 MiB (a proxy in
  front, such as Cloudflare, takes at most 100 MB a body), so a big file goes in parts; the file is published when it
  is whole and its bytes give H. **A published file is never replaced**: the same bytes again — 200 `same`, other
  bytes — 409. A part that does not start where the gathered bytes end — 409 with `X-Offset`.
- `PUT /v1/releases.json` — only from the version read (`If-Match: <ETag>`; the first one with `If-None-Match: *`).
  Every `{url, sha256, size}` in it must name a stored file with that sum and size: a release never points at a file
  that is not there.
- `PUT /min.json` — `{"team"|"open": {"version", "reason": {language: text}, "since": "YYYY-MM-DD", "stops": [...]}}`,
  under the same `If-Match` / `If-None-Match: *` as `releases.json`.
- Every write and refused write is a line of `log.jsonl` on the volume: time, path, size, sum, result, the writer's
  address.

`releases.json` as `upload.py` writes it:

```json
{"channels": {"stable": {"version": "1.9.0", "commit": "<sha>", "published_at": "2026-11-07T10:00:00Z",
                         "runtime_min": 3, "notes": "…",
                         "layer": {"url": "/v1/layer/code-1.9.0-team.tar.gz", "sha256": "…", "size": 11700000}},
              "nightly": {"…": "…"}},
 "runtime": {"current": 4, "apps": {"mac-arm64": {"url": "/v1/app/…", "sha256": "…", "size": 0, "runtime": 4}},
             "shells": {"linux-x64": {"url": "/v1/app/agents-hub-v1.9.0-linux-x64.tar.gz", "sha256": "…", "size": 0,
                                      "runtime": 4}}},
 "scripts": {"install.sh": {"url": "/v1/script/install-1.9.0.sh", "sha256": "…", "size": 0, "version": "1.9.0"},
             "install.ps1": {"url": "/v1/script/install-1.9.0.ps1", "sha256": "…", "size": 0, "version": "1.9.0"}}}
```

## The one-line install

`curl -fsSL https://<server>/i/<build key>/install.sh | bash` (Windows: `irm https://<server>/i/<build key>/install.ps1 | iex`).

- The scripts are files of `/v1/script/` named `install-<version>.sh|ps1` (printable ASCII, at most 256 KiB, never
  replaced); `releases.json` `scripts` names the current ones. The environments are files of `/v1/app/`; `runtime.shells`
  names one per platform: `mac-arm64`, `mac-x64`, `linux-x64`, `linux-arm64`, `windows-x64`, `windows-arm64`.
- `GET /i/<key>/install.sh` finds the key among the build keys (never among the release keys — a key found in both lists
  is refused) and answers `text/plain; charset=utf-8`, `Cache-Control: no-store, private`, the script with the key
  written in: where the script has `@@HUB_DELIVER_KEY@@` the key takes its place (a PowerShell `param()` block must stay
  first), else a first line is put in front — `HUB_DELIVER_KEY=<key>` (after a `#!` line) or `$HUB_DELIVER_KEY = '<key>'`.
  Any other ask under `/i/` — an unknown key, the release key, no such script, a wrong spelling — is a 404 with no body.
- The key never reaches the log: any path that reads as `/i/…` (unquoted, slashes gathered, case dropped) is logged as
  `/i/-`. Cloudflare in front sees the address, as it sees any request; the answer is `no-store`.

## Stands

A hub that came up on a release and did not go back says so, once: `POST /v1/stand {"id": "<32–64 hex>", "version":
"1.9.0"}` (`"version": null` takes it off — the owner returned it). The id is a random number the hub made for itself:
the build key is one for the whole team, so it cannot tell hubs apart. The server keeps `sha256(id) → (release, when)`
in `stands.json` on the volume, never the id, and does not write it to the log. A hub counts once and on one release (a
new word moves it); a word older than 30 days counts for nothing (hubs say it again weekly); 5000 hubs at most (507).

A reader of `GET /v1/releases.json` (the build key) finds `"stands": N` in each channel — the number of hubs on that
channel's release; the page and Telegram write "Running on N hubs" next to the "Update" button, and whoever fears the
update waits until N is high enough. The release key reads the document as stored (it writes back what it read), so the
count is never part of it. A build key that leaks can inflate the count: it is a hint for people, not a gate.

## Running

```sh
python3 server.py keygen              # a key on stdout, its sha256 on stderr: once for the build, once for releases
HUB_DELIVER_DATA=/tmp/d HUB_DELIVER_READ_KEYS=<sha256> HUB_DELIVER_WRITE_KEYS=<sha256> python3 server.py serve
docker build -t hub-deliver . && docker run -p 8080:8080 -v deliver:/data \
  -e HUB_DELIVER_READ_KEYS=<sha256> -e HUB_DELIVER_WRITE_KEYS=<sha256> hub-deliver
```

Settings: `HUB_DELIVER_DATA` (`/data`), `HUB_DELIVER_PORT` (8080), `HUB_DELIVER_HOST` (0.0.0.0),
`HUB_DELIVER_READ_KEYS` and `HUB_DELIVER_WRITE_KEYS` (sha256 hex, comma-separated — two while a key is being changed),
`HUB_DELIVER_TRUST_PROXY=1` (the writer's address from `CF-Connecting-IP` / `X-Forwarded-For`).
`server.py du` — what is stored and how much room is left; `server.py check` — the volume takes a write.

Sending from the release machine: `tools/package/upload.py <server> layer|app|shell|script|min|show …` of the core (a
look by default, `--send` for real): `shell <archive> --platform P --runtime N` puts an environment under
`runtime.shells`, `script <install.sh|install.ps1> --version V` stores the script and makes it current.
