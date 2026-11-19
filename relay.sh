#!/bin/sh
# Your own hub bus relay in one command, on a Linux server with Docker and a DNS name pointing at it:
#
#   curl -fsSL https://<the pages of a release>/relay.sh | sh -s relay.example.com
#   sh relay/install.sh relay.example.com          (from a copy of the repository: the relay of this copy)
#
# It puts a compose project into ~/hub-relay (HUB_RELAY_DIR; the project is named after the folder): the relay
# (relay/Dockerfile of the release the pages are of: Docker builds it straight from the repository at that tag) and
# Caddy in front of it (HTTPS of Let's Encrypt, renewed by itself; ports 80 and 443). Then it prints the line of the
# first hub: paste it into that hub's "Accept an invitation" (or `hub bus join -`) before it links with anyone; the next
# hubs come by its links. The line is kept in ~/hub-relay/first-hub.txt (0600). Run it again any time: the same project,
# the same line until the first hub came.
set -eu

REPO="https://github.com/icons8/hub.git"   # the release writes its repository and tag in (tools/package/pages.py); a copy from git
TAG="v1.9.9"     # keeps the marks and builds the relay/ folder next to this script

say() { printf '%s\n' "$*" >&2; }
die() { say "relay.sh: $*"; exit 1; }
compose() { docker compose --project-directory "$DIR" -f "$DIR/compose.yaml" "$@" </dev/null; }   # stdin: never the script's

main() {   # the whole script is one function, called on its last line: `curl | sh` runs it only once it came whole
  NAME=${1:-}
  DIR=${HUB_RELAY_DIR:-$HOME/hub-relay}
  [ -n "$NAME" ] || die "the relay's name: sh relay.sh relay.example.com (a DNS name that points at this server)"
  case "$NAME" in *"
"*) die "the relay's name is one line" ;; esac
  printf '%s' "$NAME" | grep -Eq '^([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]([A-Za-z0-9-]*[A-Za-z0-9])?$' \
    || die "$NAME: a DNS name, like relay.example.com (not an address: Let's Encrypt certifies names)"
  [ "$(uname -s)" = Linux ] || die "a Linux server (this is $(uname -s)): the relay runs in Docker there"
  command -v docker >/dev/null 2>&1 || die "no Docker: install it first (https://docs.docker.com/engine/install/)"
  docker info </dev/null >/dev/null 2>&1 \
    || die "Docker does not answer this user: add it to the docker group (then log in again) or run as root"
  docker compose version </dev/null >/dev/null 2>&1 || die "no Docker Compose plugin: install docker-compose-plugin"
  if command -v getent >/dev/null 2>&1 && ! getent hosts "$NAME" >/dev/null 2>&1; then
    die "$NAME does not resolve: make its DNS record point at this server first (HTTPS needs it)"
  fi

  case "$REPO" in
    @@*) here=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)
         [ -f "$here/Dockerfile" ] && [ -f "$here/server.py" ] \
           || die "run the release's relay.sh (from its pages), or sh relay/install.sh from a copy of the repository"
         BUILD=$here ;;
    *)   BUILD="$REPO#$TAG:relay" ;;
  esac

  if ! { [ -f "$DIR/compose.yaml" ] && compose ps --status running -q caddy 2>/dev/null | grep -q .; }; then
    # the ports: free, unless this very project's Caddy holds them (a run again)
    if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq '[:.](80|443)$'; then
      die "port 80 or 443 is taken on this server: Caddy needs both (HTTPS of Let's Encrypt)"
    fi
  fi

  umask 077
  mkdir -p "$DIR"
  cat > "$DIR/compose.yaml" <<EOF
# the hub bus relay and Caddy (HTTPS) in front of it: written by relay.sh; run it again rather than editing
services:
  relay:
    build: "$BUILD"
    restart: unless-stopped
    environment:
      HUB_RELAY_URL: https://$NAME
      HUB_RELAY_TRUST_PROXY: "1"
    volumes:
      - relay-data:/data
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    depends_on:
      - relay
volumes:
  relay-data: {}
  caddy-data: {}
  caddy-config: {}
EOF
  printf '%s {\n\treverse_proxy relay:8080\n}\n' "$NAME" > "$DIR/Caddyfile"
  chmod 644 "$DIR/Caddyfile"   # read by Caddy's own user in its container

  say "relay.sh: building and starting the relay at https://$NAME (the first time: a few minutes)"
  compose up -d --build >&2

  i=0   # /healthz answers once the server listens; its key is made by then (another process would race it for the key)
  until compose exec -T relay python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 60 ] || die "the relay did not come up: docker compose --project-directory $DIR logs relay"
    sleep 2
  done

  set +e
  line=$(compose exec -T relay python server.py bootstrap 2>"$DIR/bootstrap.err")
  code=$?
  set -e
  if [ "$code" -eq 1 ] && grep -q "already on this relay" "$DIR/bootstrap.err"; then
    rm -f "$DIR/bootstrap.err" "$DIR/first-hub.txt"   # its line was used: no file to paste from again
    say "relay.sh: the relay at https://$NAME works and has hubs already: a new hub comes by a link of one of them."
    say "Another first line anyway: docker compose --project-directory $DIR exec relay python server.py invite"
    exit 0
  fi
  if [ "$code" -ne 0 ] || [ -z "$line" ]; then
    cat "$DIR/bootstrap.err" >&2
    die "the relay gave no line for the first hub (its words above); its log: docker compose --project-directory $DIR logs relay"
  fi
  rm -f "$DIR/bootstrap.err"
  printf '%s\n' "$line" > "$DIR/first-hub.txt"
  say ""
  say "The relay is at https://$NAME (HTTPS comes from Let's Encrypt within a minute or two; if it does not, the"
  say "name's DNS record does not point at this server)."
  say "The line below is for your first hub: paste it into \"Accept an invitation\" (Settings, External hubs) or give it"
  say "to hub bus join - . The next hubs come by that hub's links. The line is kept in $DIR/first-hub.txt."
  say ""
  printf '%s\n' "$line"
}

main "$@"
